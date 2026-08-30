"""Promotion orchestration -- the BRD section 6 end-to-end sequence.

The user creates the staging branch before dispatch. The promotion inventory is
read from that branch, requested files are read from ``origin/<source>``, and
only the supplied staging branch may be updated. Remote writes are deferred
until every validation and the change-set guard have passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import config as config_mod
from . import guards, inventory as inventory_mod, workflows_list
from .config import Config, Environment
from .errors import (
    E_BAD_DELETE,
    E_BRANCH_MISSING,
    E_GIT,
    E_MISSING_SOURCE,
    E_NO_STAGING_BRANCH,
    E_NOT_A_FILE,
    E_PROMOTION_FILE_MISSING,
    PromotionError,
)
from .gitops import Git
from .inventory import Inventory
from .pr import GhCliBackend, PullRequest, RecordingBackend, render_body, render_title

TIMESTAMP_FORMAT = "%d_%m_%Y_%H_%M_%S"
PROMOTION_FILENAME = "promotion.txt"


class PrBackend(Protocol):
    def create(self, pr: PullRequest) -> str: ...


@dataclass(frozen=True)
class PromotionResult:
    environment: str
    source_branch: str
    target_branch: str
    staging_branch: str
    base_sha: str
    timestamp: str
    commit_sha: str | None
    changes: list[tuple[str, str]] = field(default_factory=list)
    workflows_list_entries: list[str] | None = None
    pr_url: str = ""
    pr: PullRequest | None = None
    dry_run: bool = False


def make_timestamp(
    now: datetime | None = None, tz: timezone = timezone.utc
) -> str:
    """Audit identifier, ``DD_MM_YYYY_HH_MM_SS`` (BRD sections 5 and 16).

    Stamped in ``tz`` so PR titles and audit records read against the wall clock
    of whoever dispatched the run. Runners are UTC, so without this the value
    trails local time by the UTC offset and looks stale.
    """
    moment = now or datetime.now(tz)
    return moment.astimezone(tz).strftime(TIMESTAMP_FORMAT)


def _preflight_paths(
    git: Git,
    inv: Inventory,
    env: Environment,
    base_sha: str,
    staging_branch: str,
) -> None:
    """Validate every requested path against the repository (section 15).

    Each category reports *all* offending paths, so one re-run can fix them all.
    """
    source_rev = f"refs/remotes/{git.remote}/{env.source}"

    missing: list[str] = []
    not_a_file: list[str] = []
    for entry in inv.promotes:
        kind = git.object_type(source_rev, entry.path)
        if kind is None:
            missing.append(f"{entry.location}: {entry.path}")
        elif kind != "blob":
            not_a_file.append(f"{entry.location}: {entry.path} (is a {kind})")

    if missing:
        raise PromotionError(
            E_MISSING_SOURCE,
            f"{len(missing)} requested file(s) do not exist on the source branch "
            f"'{env.source}'.",
            details=missing,
            remedy=f"Confirm the paths exist on '{env.source}' and are spelled "
            f"exactly as in the repository, then re-run.",
        )
    if not_a_file:
        raise PromotionError(
            E_NOT_A_FILE,
            f"{len(not_a_file)} requested path(s) are not files.",
            details=not_a_file,
            remedy="List individual file paths. Directories are not promoted as "
            "a unit.",
        )

    bad_deletes: list[str] = []
    for entry in inv.deletes:
        kind = git.object_type(base_sha, entry.path)
        if kind is None:
            bad_deletes.append(
                f"{entry.location}: {entry.path} (not present on "
                f"staging branch '{staging_branch}')"
            )
        elif kind != "blob":
            bad_deletes.append(
                f"{entry.location}: {entry.path} (is a {kind} on "
                f"staging branch '{staging_branch}')"
            )

    if bad_deletes:
        raise PromotionError(
            E_BAD_DELETE,
            f"{len(bad_deletes)} DELETE path(s) cannot be deleted.",
            details=bad_deletes,
            remedy="A DELETE path must be an existing file on the supplied "
            f"staging branch '{staging_branch}'.",
        )


def promote(
    *,
    repo_root: Path,
    deployment_target: str | None,
    staging_branch: str | None,
    release_description: str | None = None,
    cfg: Config | None = None,
    git: Git | None = None,
    pr_backend: PrBackend | None = None,
    now: datetime | None = None,
    run_url: str | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda _msg: None,
) -> PromotionResult:
    repo_root = Path(repo_root)
    cfg = cfg or config_mod.load(repo_root)
    git = git or Git(repo_root)
    backend: PrBackend = pr_backend or (
        RecordingBackend() if dry_run else GhCliBackend(cwd=repo_root)
    )

    # 1. Resolve the route and validate the supplied branch name.
    env = cfg.resolve(deployment_target)
    if not staging_branch or not staging_branch.strip():
        raise PromotionError(
            E_NO_STAGING_BRANCH,
            "No staging branch was supplied.",
            remedy="Re-run the workflow and select the user-created staging branch "
            "that contains promotion.txt.",
        )
    staging_branch = config_mod.validate_branch_name(
        staging_branch.strip(), "staging_branch"
    )

    # 2. Refresh remote refs.
    git.fetch()

    dirty = git.out("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PromotionError(
            E_GIT,
            "The checkout has uncommitted changes; refusing to build a "
            "promotion on top of them.",
            details=dirty.splitlines(),
        )

    # 3. The configured route and supplied staging branch must exist remotely.
    heads = git.remote_heads()
    absent = [b for b in (env.source, env.target, staging_branch) if b not in heads]
    if absent:
        raise PromotionError(
            E_BRANCH_MISSING,
            f"Required branch(es) do not exist on the remote: "
            f"{', '.join(absent)}.",
            details=[
                f"{b} ({'staging branch input' if b == staging_branch else f'environments.{env.name}'})"
                for b in absent
            ],
            remedy="Create the user-supplied staging branch or correct "
            f"{config_mod.CONFIG_FILENAME}.",
        )

    # 4. The supplied branch is the only branch this run may push.
    guards.assert_push_allowed(staging_branch, cfg)
    staging_rev = f"refs/remotes/{git.remote}/{staging_branch}"
    base_sha = git.remote_branch_sha(staging_branch)
    log(f"Staging baseline: {staging_branch} @ {base_sha}")

    # 5. Read and validate the root inventory before changing the checkout.
    if git.object_type(staging_rev, PROMOTION_FILENAME) != "blob":
        raise PromotionError(
            E_PROMOTION_FILE_MISSING,
            f"{PROMOTION_FILENAME} was not found in staging branch '{staging_branch}'.",
            remedy=f"Add {PROMOTION_FILENAME} at the repository root of "
            f"'{staging_branch}', commit it, and re-run.",
        )
    inv = inventory_mod.parse(git.read_file_text(staging_rev, PROMOTION_FILENAME), cfg)
    log(
        f"Promoting {len(inv.entries)} path(s) to {env.name}: "
        f"{env.source} -> {env.target} through {staging_branch} "
        f"({len(inv.promotes)} to promote, {len(inv.deletes)} to delete)"
    )

    # 6. Timestamp is retained for auditing and PR titles only.
    timestamp = make_timestamp(now, cfg.timestamp_tz)

    # 7. Repository-level validation, still before any write.
    _preflight_paths(git, inv, env, base_sha, staging_branch)

    # 8. Check out the existing staging branch at the fetched remote commit.
    git.checkout_existing_branch(staging_branch, base_sha)

    # 9. Apply source files to the same staging branch.
    if inv.promote_paths:
        git.checkout_paths_from(f"refs/remotes/{git.remote}/{env.source}", inv.promote_paths)
        log(f"Applied {len(inv.promote_paths)} file(s) from {env.source}")
    if inv.delete_paths:
        git.remove_paths(inv.delete_paths)
        log(f"Deleted {len(inv.delete_paths)} file(s)")

    # 10. The workflows_list.txt rebuild rule (section 10).
    desired = workflows_list.desired_content(inv, cfg)
    list_entries: list[str] | None = None
    if desired is None:
        log(
            f"{cfg.workflows_list_file}: unchanged (no workflow paths promoted "
            f"by this request)"
        )
    else:
        list_path = repo_root / cfg.workflows_list_file
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text(desired, encoding="utf-8", newline="\n")
        git.add_paths([cfg.workflows_list_file])
        # Verify what will actually be committed, not the working-tree bytes:
        # on Windows checkouts autocrlf can make those differ.
        workflows_list.verify(git.read_index_text(cfg.workflows_list_file), inv, cfg)
        list_entries = desired.splitlines()
        log(f"{cfg.workflows_list_file}: rebuilt with {len(list_entries)} entry(ies)")

    # 11. Nothing outside the requested set may have changed (sections 6, 20).
    changes = git.staged_changes()
    guards.assert_changes_expected(
        changes, inv.all_paths, cfg, allow_workflows_list=desired is not None
    )

    # 12. Commit.
    commit_message = "\n".join(
        [
            f"Promote {len(inv.entries)} path(s) to {env.name} [{timestamp}]",
            "",
            f"Source branch: {env.source}",
            f"Target branch: {env.target}",
            f"Staging branch: {staging_branch}",
            f"Staging baseline commit: {base_sha}",
            f"Inventory: {PROMOTION_FILENAME}",
        ]
    )
    commit_sha = git.commit(commit_message)
    log(f"Committed {commit_sha} with {len(changes)} change(s)")

    pull = PullRequest(
        title=render_title(env.name, timestamp),
        body=render_body(
            env_name=env.name,
            source_branch=env.source,
            target_branch=env.target,
            base_sha=base_sha,
            timestamp=timestamp,
            staging_branch=staging_branch,
            changes=changes,
            requested_promotes=inv.promote_paths,
            requested_deletes=inv.delete_paths,
            workflows_list_file=cfg.workflows_list_file,
            workflows_list_entries=list_entries,
            release_description=release_description,
            run_url=run_url,
        ),
        base=env.target,
        head=staging_branch,
    )

    result_kwargs = dict(
        environment=env.name,
        source_branch=env.source,
        target_branch=env.target,
        staging_branch=staging_branch,
        base_sha=base_sha,
        timestamp=timestamp,
        commit_sha=commit_sha,
        changes=changes,
        workflows_list_entries=list_entries,
        pr=pull,
    )

    if dry_run:
        log("Dry run: stopping before any push.")
        return PromotionResult(**result_kwargs, pr_url="", dry_run=True)

    # 13. Publish the existing staging branch, then open the Pull Request.
    git.push_existing_branch(staging_branch)
    log(f"Pushed {staging_branch}")

    pr_url = backend.create(pull)
    log(f"Pull Request: {pr_url}")

    return PromotionResult(**result_kwargs, pr_url=pr_url)
