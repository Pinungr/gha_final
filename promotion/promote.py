"""Promotion orchestration -- the BRD section 6 end-to-end sequence.

Reads happen against ``origin/<source>``; the only branch ever written is the
generated temporary branch. The remote pushes are deliberately deferred until
after every validation and the change-set guard have passed, so a failed run
leaves no stray branches behind and no protected branch touched.
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
    E_BRANCH_EXISTS,
    E_BRANCH_MISSING,
    E_GIT,
    E_MISSING_SOURCE,
    E_NOT_A_FILE,
    PromotionError,
)
from .gitops import Git
from .inventory import Inventory
from .pr import GhCliBackend, PullRequest, RecordingBackend, render_body, render_title

TIMESTAMP_FORMAT = "%d_%m_%Y_%H_%M_%S"


class PrBackend(Protocol):
    def create(self, pr: PullRequest) -> str: ...


@dataclass(frozen=True)
class PromotionResult:
    environment: str
    source_branch: str
    target_branch: str
    base_sha: str
    timestamp: str
    temp_branch: str
    release_branch: str
    commit_sha: str | None
    changes: list[tuple[str, str]] = field(default_factory=list)
    workflows_list_entries: list[str] | None = None
    pr_url: str = ""
    pr: PullRequest | None = None
    dry_run: bool = False


def make_timestamp(
    now: datetime | None = None, tz: timezone = timezone.utc
) -> str:
    """Execution identifier, ``DD_MM_YYYY_HH_MM_SS`` (BRD sections 5 and 16).

    Stamped in ``tz`` so branch names read against the wall clock of whoever
    dispatched the run. Runners are UTC, so without this the name trails local
    time by the UTC offset and looks stale.
    """
    moment = now or datetime.now(tz)
    return moment.astimezone(tz).strftime(TIMESTAMP_FORMAT)


def _preflight_paths(
    git: Git, inv: Inventory, env: Environment, base_sha: str
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
                f"'{env.target}')"
            )
        elif kind != "blob":
            bad_deletes.append(
                f"{entry.location}: {entry.path} (is a {kind} on "
                f"'{env.target}')"
            )

    if bad_deletes:
        raise PromotionError(
            E_BAD_DELETE,
            f"{len(bad_deletes)} DELETE path(s) cannot be deleted.",
            details=bad_deletes,
            remedy=f"A DELETE path must be an existing file on the target branch "
            f"'{env.target}'.",
        )


def _assert_branches_available(
    git: Git, temp_branch: str, release_branch: str
) -> None:
    heads = git.remote_heads()
    clash = [b for b in (temp_branch, release_branch) if b in heads]
    if clash:
        raise PromotionError(
            E_BRANCH_EXISTS,
            "The generated branch name(s) already exist on the remote.",
            details=clash,
            remedy="Re-run the workflow; branch names are derived from the "
            "execution time and a new run generates new names.",
        )


def promote(
    *,
    repo_root: Path,
    deployment_target: str | None,
    files_to_promote: str | None,
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

    # 1-2. Resolve the route and validate the inventory before touching git.
    env = cfg.resolve(deployment_target)
    inv = inventory_mod.parse(files_to_promote, cfg)
    log(
        f"Promoting {len(inv.entries)} path(s) to {env.name}: "
        f"{env.source} -> {env.target} "
        f"({len(inv.promotes)} to promote, {len(inv.deletes)} to delete)"
    )

    # 3. Refresh remote refs.
    git.fetch()

    dirty = git.out("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PromotionError(
            E_GIT,
            "The checkout has uncommitted changes; refusing to build a "
            "promotion on top of them.",
            details=dirty.splitlines(),
        )

    # 4. Both configured branches must exist on the remote.
    heads = git.remote_heads()
    absent = [b for b in (env.source, env.target) if b not in heads]
    if absent:
        raise PromotionError(
            E_BRANCH_MISSING,
            f"Configured branch(es) do not exist on the remote: "
            f"{', '.join(absent)}.",
            details=[f"{b} (from environments.{env.name})" for b in absent],
            remedy=f"Create the branch(es), or correct "
            f"{config_mod.CONFIG_FILENAME}.",
        )

    # 5. One baseline commit for both generated branches (section 5.1).
    base_sha = git.remote_branch_sha(env.target)
    log(f"Baseline: {env.target} @ {base_sha}")

    # 6. One timestamp for both generated branches (sections 5 and 16).
    timestamp = make_timestamp(now, config.timestamp_tz)
    temp_branch = f"temp/{timestamp}_{env.slug}"
    release_branch = f"release/{timestamp}_{env.slug}"
    log(f"Branches: {temp_branch} and {release_branch}")

    # 7. Repository-level validation, still before any write.
    _preflight_paths(git, inv, env, base_sha)
    _assert_branches_available(git, temp_branch, release_branch)
    for branch in (temp_branch, release_branch):
        guards.assert_push_allowed(branch, cfg)

    # 8. The temporary branch, off the captured baseline.
    git.checkout_new_branch(temp_branch, base_sha)

    # 9. Apply the requested changes -- to the temporary branch only.
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
            f"Baseline commit: {base_sha}",
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
            temp_branch=temp_branch,
            release_branch=release_branch,
            changes=changes,
            requested_promotes=inv.promote_paths,
            requested_deletes=inv.delete_paths,
            workflows_list_file=cfg.workflows_list_file,
            workflows_list_entries=list_entries,
            release_description=release_description,
            run_url=run_url,
        ),
        base=release_branch,
        head=temp_branch,
    )

    result_kwargs = dict(
        environment=env.name,
        source_branch=env.source,
        target_branch=env.target,
        base_sha=base_sha,
        timestamp=timestamp,
        temp_branch=temp_branch,
        release_branch=release_branch,
        commit_sha=commit_sha,
        changes=changes,
        workflows_list_entries=list_entries,
        pr=pull,
    )

    if dry_run:
        log("Dry run: stopping before any push.")
        return PromotionResult(**result_kwargs, pr_url="", dry_run=True)

    # 13. Publish both generated branches, then open the Pull Request.
    git.push_new_branch(base_sha, release_branch)
    git.push_new_branch(commit_sha, temp_branch)
    log(f"Pushed {release_branch} and {temp_branch}")

    pr_url = backend.create(pull)
    log(f"Pull Request: {pr_url}")

    return PromotionResult(**result_kwargs, pr_url=pr_url)
