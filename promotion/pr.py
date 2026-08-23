"""Pull Request creation (BRD sections 13 and 17).

The PR is the review and approval record, so its body carries everything needed
to audit the promotion later: both branch names, the baseline commit, the
execution timestamp, and every requested path with its resulting change.

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
            raise PromotionError(
                E_GH,
                f"Creating the Pull Request failed (gh exit code "
                f"{proc.returncode}).",
                details=[
                    line
                    for line in (proc.stderr or "").splitlines()
                    if line.strip()
                ],
                remedy=f"The branches {pr.head} and {pr.base} were pushed. Check "
                f"the token's 'pull-requests: write' permission, then open the "
                f"Pull Request manually if needed.",
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
    temp_branch: str,
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
        f"| Target branch (baseline) | `{target_branch}` |",
        f"| Baseline commit | `{base_sha}` |",
        f"| Execution timestamp | `{timestamp}` |",
        f"| Temporary branch | `{temp_branch}` |",
        f"| Release branch | `{release_branch}` |",
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
        "not modified. Squash and merge this Pull Request once the configured "
        f"approvals are satisfied to publish the change to `{release_branch}`.",
    ]
    return "\n".join(lines)
