"""Pull Request creation (BRD sections 13 and 17).

The PR is the review and approval record, so its body carries everything needed
to audit the promotion later: the source, target, temporary and release
branches, the baseline commit, the execution timestamp, and every requested
path with its resulting change.

Creation is isolated behind :class:`PrBackend` so the orchestrator can be tested
end-to-end against local git repositories without the ``gh`` CLI or network.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import E_GH, PromotionError

_STATUS_LABEL = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "T": "type changed",
}


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str
    base: str
    head: str


_ACTIONS_PR_BLOCKED = "not permitted to create or approve pull requests"


def _manual_pr_url(pr: PullRequest) -> str | None:
    """Direct 'open a Pull Request' link, built from the runner's env."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not server or not repo:
        return None
    return f"{server}/{repo}/compare/{pr.base}...{pr.head}?expand=1"


def _gh_remedy(pr: PullRequest, stderr: str) -> str:
    """Section 15: name the actual cause, not a plausible-sounding one."""
    pushed = f"Both branches were pushed, so no work is lost: {pr.head} and {pr.base}."
    manual = _manual_pr_url(pr)
    open_it = f" Open the Pull Request here: {manual}" if manual else (
        f" Open the Pull Request manually from {pr.head} into {pr.base}."
    )

    if _ACTIONS_PR_BLOCKED in stderr:
        # The workflow already grants 'pull-requests: write'. This failure is a
        # repository/organisation setting that overrides the token, so pointing
        # at the permission block would send the reader down the wrong path.
        return (
            f"{pushed} GitHub Actions is blocked from opening Pull Requests by a "
            f"repository setting, not by the token. Enable Settings -> Actions -> "
            f"General -> Workflow permissions -> 'Allow GitHub Actions to create "
            f"and approve pull requests', then re-run.{open_it}"
        )
    return (
        f"{pushed} Check the token's 'pull-requests: write' permission and that "
        f"Actions may open Pull Requests in this repository.{open_it}"
    )


@dataclass
class GhCliBackend:
    """Creates the PR with the ``gh`` CLI, preinstalled on GitHub runners."""

    cwd: Path

    def create(self, pr: PullRequest) -> str:
        env = dict(os.environ)
        env.setdefault("GH_PROMPT_DISABLED", "1")
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            [
                "gh",
                "pr",
                "create",
                "--base",
                pr.base,
                "--head",
                pr.head,
                "--title",
                pr.title,
                "--body-file",
                "-",
            ],
            cwd=str(self.cwd),
            env=env,
            input=pr.body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            stderr = proc.stderr or ""
            raise PromotionError(
                E_GH,
                f"Creating the Pull Request failed (gh exit code "
                f"{proc.returncode}).",
                details=[
                    line for line in stderr.splitlines() if line.strip()
                ],
                remedy=_gh_remedy(pr, stderr),
            )
        return (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""


@dataclass
class RecordingBackend:
    """Records the PR instead of creating it. Used by ``--dry-run`` and tests."""

    created: list[PullRequest] = field(default_factory=list)

    def create(self, pr: PullRequest) -> str:
        self.created.append(pr)
        return f"(dry-run) would open {pr.head} -> {pr.base}"


def render_title(env_name: str, timestamp: str) -> str:
    return f"Release to {env_name}: {timestamp}"


def render_body(
    *,
    env_name: str,
    source_branch: str,
    target_branch: str,
    base_sha: str,
    timestamp: str,
    staging_branch: str,
    release_branch: str,
    changes: list[tuple[str, str]],
    requested_promotes: list[str],
    requested_deletes: list[str],
    workflows_list_file: str,
    workflows_list_entries: list[str] | None,
    release_description: str | None,
    run_url: str | None,
) -> str:
    status_by_path = {path: status for status, path in changes}
    lines: list[str] = []

    lines += [
        f"Automated promotion to **{env_name}**.",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Target environment | `{env_name}` |",
        f"| Source branch | `{source_branch}` |",
        f"| Target branch | `{target_branch}` |",
        f"| Baseline commit | `{base_sha}` |",
        f"| Execution timestamp | `{timestamp}` |",
        f"| Temporary branch | `{staging_branch}` |",
        f"| Release branch | `{release_branch}` |",
        f"| Inventory | `promotion.txt` |",
    ]
    if run_url:
        lines.append(f"| Workflow run | {run_url} |")
    lines.append("")

    total = len(requested_promotes) + len(requested_deletes)
    lines += [
        f"## Requested paths ({total})",
        "",
        "| Path | Requested | Result |",
        "| --- | --- | --- |",
    ]
    for path in requested_promotes:
        status = status_by_path.get(path)
        result = _STATUS_LABEL.get((status or "")[:1], "no change")
        lines.append(f"| `{path}` | promote | {result} |")
    for path in requested_deletes:
        status = status_by_path.get(path)
        result = _STATUS_LABEL.get((status or "")[:1], "no change")
        lines.append(f"| `{path}` | delete | {result} |")
    lines.append("")

    lines.append(f"## `{workflows_list_file}`")
    lines.append("")
    if workflows_list_entries is None:
        lines.append(
            "Not modified -- this request promotes no workflow paths."
        )
    else:
        lines.append(
            f"Rebuilt from the {len(workflows_list_entries)} workflow path(s) "
            f"promoted by this request. Previous contents were discarded."
        )
        lines.append("")
        lines += [f"- `{entry}`" for entry in workflows_list_entries]
    lines.append("")

    if release_description and release_description.strip():
        lines += ["## Release description", "", release_description.strip(), ""]

    lines += [
        "---",
        "",
        f"The source-of-truth branches (`{source_branch}`, `{target_branch}`) were "
        "not modified by the automation. Squash and merge this Pull Request once "
        f"the configured approvals are satisfied to publish the change to `{release_branch}`.",
    ]
    return "\n".join(lines)
