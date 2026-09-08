"""Event-driven promotion lifecycle helpers.

The promotion engine only prepares the initial Pull Request.  This module owns
the state carried across later GitHub Actions runs: approval, deployment,
Environment validation and (for PSUP/PROD) final synchronization.
State lives in machine-readable PR metadata and PR comments, never in a
protected application branch.
"""

from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from . import config as config_mod

MANAGED_MARKER = "<!-- dbx-promotion-managed -->"
FINAL_MARKER = "<!-- dbx-promotion-final-sync -->"
_METADATA_RE = re.compile(r"<!-- dbx-promotion-metadata: (?P<json>.+?) -->")
_STATE_RE = re.compile(r"<!-- dbx-promotion-state: (?P<json>.+?) -->")
_PROMOTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class LifecycleState(StrEnum):
    WAITING_FOR_PR_APPROVAL = "WAITING_FOR_PR_APPROVAL"
    INITIAL_PR_APPROVED = "INITIAL_PR_APPROVED"
    INITIAL_PR_MERGED = "INITIAL_PR_MERGED"
    DEPLOYMENT_TRIGGERED = "DEPLOYMENT_TRIGGERED"
    DEPLOYMENT_SUCCEEDED = "DEPLOYMENT_SUCCEEDED"
    DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"
    WAITING_FOR_VALIDATION = "WAITING_FOR_VALIDATION"
    VALIDATION_APPROVED = "VALIDATION_APPROVED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    ROLLBACK_TRIGGERED = "ROLLBACK_TRIGGERED"
    ROLLBACK_SUCCEEDED = "ROLLBACK_SUCCEEDED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    FINAL_PR_CREATED = "FINAL_PR_CREATED"
    FINAL_PR_MERGED = "FINAL_PR_MERGED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TERMINAL_STATES = {
    LifecycleState.DEPLOYMENT_FAILED,
    LifecycleState.VALIDATION_REJECTED,
    LifecycleState.ROLLBACK_SUCCEEDED,
    LifecycleState.ROLLBACK_FAILED,
    LifecycleState.FINAL_PR_MERGED,
    LifecycleState.COMPLETED,
    LifecycleState.FAILED,
}


@dataclass(frozen=True)
class PromotionMetadata:
    promotion_id: str
    target: str
    staging_branch: str
    release_branch: str | None
    deployment_branch: str
    deployment_action: str
    has_workflow_changes: bool
    initial_pr_base: str
    base_sha: str
    promotion_run_url: str | None = None
    signature: str = ""


@dataclass(frozen=True)
class LifecycleRecord:
    promotion_id: str
    state: LifecycleState
    recorded_at: str
    data: dict[str, str | bool | None]


@dataclass(frozen=True)
class InitialPrProgress:
    result: str
    merged_sha: str = ""
    merged_branch: str = ""


@dataclass(frozen=True)
class FinalPrProgress:
    result: str
    number: str
    url: str
    merge_sha: str = ""


def deployment_action_for(has_workflow_changes: bool) -> str:
    """Map the already-calculated PR workflow result to a DBX action."""
    return "create/update_workflow" if has_workflow_changes else "create/update_repo"


def make_promotion_id(run_id: str | None, timestamp: str) -> str:
    """Use the originating workflow run when available, with a safe local fallback."""
    candidate = f"run-{run_id}" if run_id else f"promotion-{timestamp}"
    if not _PROMOTION_ID_RE.fullmatch(candidate):
        raise ValueError("promotion correlation identifier contains unsupported characters")
    return candidate


def metadata_comment(metadata: PromotionMetadata) -> str:
    return f"<!-- dbx-promotion-metadata: {json.dumps(asdict(metadata), sort_keys=True)} -->"


def sign_metadata(metadata: PromotionMetadata, secret: str) -> PromotionMetadata:
    """Attach a stable HMAC so ordinary PR authors cannot forge managed metadata."""
    if not secret:
        return metadata
    payload = asdict(metadata)
    payload["signature"] = ""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()
    return PromotionMetadata(**{**payload, "signature": signature})


def metadata_is_authenticated(metadata: PromotionMetadata, secret: str) -> bool:
    if not secret or not metadata.signature:
        return False
    return hmac.compare_digest(sign_metadata(
        PromotionMetadata(**{**asdict(metadata), "signature": ""}), secret
    ).signature, metadata.signature)


def state_comment(record: LifecycleRecord) -> str:
    payload = {"promotion_id": record.promotion_id, "state": record.state.value,
               "recorded_at": record.recorded_at, "data": record.data}
    return f"<!-- dbx-promotion-state: {json.dumps(payload, sort_keys=True)} -->"


def parse_metadata(body: str) -> PromotionMetadata | None:
    if MANAGED_MARKER not in body or FINAL_MARKER in body:
        return None
    match = _METADATA_RE.search(body)
    if not match:
        return None
    try:
        raw = json.loads(match.group("json"))
        metadata = PromotionMetadata(**raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return metadata if _PROMOTION_ID_RE.fullmatch(metadata.promotion_id) else None


def parse_final_metadata(body: str) -> PromotionMetadata | None:
    """Parse a final-sync marker without treating it as an initial PR."""
    if FINAL_MARKER not in body or MANAGED_MARKER not in body:
        return None
    match = _METADATA_RE.search(body)
    if not match:
        return None
    try:
        metadata = PromotionMetadata(**json.loads(match.group("json")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return metadata if _PROMOTION_ID_RE.fullmatch(metadata.promotion_id) else None


def parse_state(comment: str) -> LifecycleRecord | None:
    match = _STATE_RE.search(comment)
    if not match:
        return None
    try:
        raw = json.loads(match.group("json"))
        return LifecycleRecord(
            promotion_id=raw["promotion_id"],
            state=LifecycleState(raw["state"]),
            recorded_at=raw["recorded_at"],
            data=raw.get("data", {}),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class GhClient(Protocol):
    def api(self, endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None) -> Any: ...

    def command(self, *args: str) -> str: ...

    def api_all(self, endpoint: str) -> list[dict[str, Any]]: ...


@dataclass
class GhCli:
    """Small, testable adapter around the GitHub CLI; no shell is used."""

    repo: str

    def command(self, *args: str) -> str:
        env = dict(os.environ)
        env.setdefault("GH_PROMPT_DISABLED", "1")
        proc = subprocess.run(["gh", *args], check=False, capture_output=True, text=True, env=env)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout).strip())
        return (proc.stdout or "").strip()

    def api(self, endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None) -> Any:
        args = ["api", endpoint, "--method", method]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        text = self.command(*args)
        return json.loads(text) if text else None

    def api_all(self, endpoint: str) -> list[dict[str, Any]]:
        data = json.loads(self.command("api", endpoint, "--paginate", "--slurp"))
        return [item for page in data for item in page] if isinstance(data, list) else []


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _comment(client: GhClient, number: int, record: LifecycleRecord) -> None:
    client.api(f"repos/{_repo()}/issues/{number}/comments", method="POST", fields={"body": state_comment(record)})


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is required")
    return repo


def _metadata_from_pr(pr: dict[str, Any]) -> PromotionMetadata | None:
    metadata = parse_metadata(str(pr.get("body") or ""))
    if not metadata or not metadata_is_authenticated(
        metadata, os.environ.get("PROMOTION_LIFECYCLE_HMAC_KEY", "")
    ):
        return None
    return metadata


def _latest_record(comments: list[dict[str, Any]], promotion_id: str) -> LifecycleRecord | None:
    for comment in reversed(comments):
        record = parse_state(str(comment.get("body") or ""))
        if record and record.promotion_id == promotion_id:
            return record
    return None


def _comments(client: GhClient, number: int) -> list[dict[str, Any]]:
    data = client.api(f"repos/{_repo()}/issues/{number}/comments?per_page=100")
    return data if isinstance(data, list) else []


def _record(client: GhClient, number: int, promotion_id: str, state: LifecycleState, **data: str | bool | None) -> None:
    _comment(client, number, LifecycleRecord(promotion_id, state, _iso(_now()), data))


def _record_once(
    client: GhClient, number: int, promotion_id: str, state: LifecycleState, **data: str | bool | None
) -> None:
    current = _latest_record(_comments(client, number), promotion_id)
    if not current or current.state != state:
        _record(client, number, promotion_id, state, **data)


def _require_sha(value: str, label: str) -> str:
    if not _SHA_RE.fullmatch(value):
        raise RuntimeError(f"{label} must be a full 40-character commit SHA")
    return value.lower()


def _managed_pr(client: GhClient, promotion_id: str, number: int) -> tuple[PromotionMetadata, dict[str, Any]]:
    pr = client.api(f"repos/{_repo()}/pulls/{number}")
    if not isinstance(pr, dict) or int(pr.get("number") or 0) != number:
        raise RuntimeError("Pull Request could not be loaded by its expected number")
    metadata = _metadata_from_pr(pr)
    if not metadata or metadata.promotion_id != promotion_id:
        raise RuntimeError("Pull Request is not the authenticated managed promotion")
    return metadata, pr


def _assert_initial_identity(
    metadata: PromotionMetadata,
    pr: dict[str, Any],
    staging_branch: str,
    initial_pr_base: str,
    expected_head_sha: str,
) -> None:
    if (
        metadata.staging_branch != staging_branch
        or metadata.initial_pr_base != initial_pr_base
        or str((pr.get("head") or {}).get("ref") or "") != staging_branch
        or str((pr.get("base") or {}).get("ref") or "") != initial_pr_base
    ):
        raise RuntimeError("initial Pull Request branch identity changed unexpectedly")
    if str((pr.get("head") or {}).get("sha") or "").lower() != _require_sha(expected_head_sha, "initial PR head SHA"):
        raise RuntimeError("initial Pull Request head SHA changed unexpectedly")


def _branch_sha(client: GhClient, branch: str) -> str:
    ref = client.api(f"repos/{_repo()}/git/ref/heads/{branch}") or {}
    return _require_sha(str((ref.get("object") or {}).get("sha") or ""), f"branch {branch!r} HEAD")


def advance_initial_pr(
    client: GhClient,
    promotion_id: str,
    number: int,
    expected_head_sha: str,
    staging_branch: str,
    initial_pr_base: str,
) -> InitialPrProgress:
    """Request normal auto-merge and report the actual merge state."""
    metadata, pr = _managed_pr(client, promotion_id, number)
    _assert_initial_identity(metadata, pr, staging_branch, initial_pr_base, expected_head_sha)
    if pr.get("merged"):
        return InitialPrProgress(
            "merged",
            _require_sha(str(pr.get("merge_commit_sha") or ""), "merged initial PR SHA"),
            metadata.deployment_branch,
        )
    if str(pr.get("state") or "open").lower() != "open" or pr.get("draft"):
        raise RuntimeError("initial Pull Request was closed without merging or is still a draft")
    current = _latest_record(_comments(client, number), promotion_id)
    if not current or current.state != LifecycleState.INITIAL_PR_APPROVED:
        _record(client, number, promotion_id, LifecycleState.INITIAL_PR_APPROVED)
        # No --admin and no branch deletion: repository protection remains authoritative.
        client.command(
            "pr", "merge", str(number), "--repo", _repo(), "--squash", "--auto",
            "--match-head-commit", _require_sha(expected_head_sha, "initial PR head SHA"),
        )
    return InitialPrProgress("waiting")


def verify_initial_merge(
    client: GhClient,
    promotion_id: str,
    number: int,
    merged_sha: str,
    deployment_branch: str,
    deployment_target: str,
    deployment_action: str,
) -> None:
    metadata, pr = _managed_pr(client, promotion_id, number)
    expected_sha = _require_sha(merged_sha, "merged initial PR SHA")
    current = _latest_record(_comments(client, number), promotion_id)
    if (
        not pr.get("merged")
        or _require_sha(str(pr.get("merge_commit_sha") or ""), "merged initial PR SHA") != expected_sha
        or metadata.target != deployment_target
        or metadata.deployment_branch != deployment_branch
        or metadata.deployment_action != deployment_action
        or not current
        or current.state not in {LifecycleState.INITIAL_PR_APPROVED, LifecycleState.INITIAL_PR_MERGED}
    ):
        raise RuntimeError("initial Pull Request merge does not match the trusted promotion inputs")
    _record_once(
        client,
        number,
        promotion_id,
        LifecycleState.INITIAL_PR_MERGED,
        deployment_branch=deployment_branch,
        deployment_sha=expected_sha,
    )


def verify_deployment_branch(client: GhClient, deployment_branch: str, deployment_sha: str) -> None:
    if _branch_sha(client, deployment_branch) != _require_sha(deployment_sha, "deployment SHA"):
        raise RuntimeError("deployment branch HEAD differs from the merged promotion SHA")


def record_deployment_completed(
    client: GhClient,
    promotion_id: str,
    number: int,
    deployment_target: str,
    deployment_branch: str,
    expected_sha: str,
    actual_result: str,
    actual_sha: str,
    cfg_path: Path,
) -> tuple[bool, str]:
    metadata, pr = _managed_pr(client, promotion_id, number)
    expected_sha = _require_sha(expected_sha, "expected deployment SHA")
    current = _latest_record(_comments(client, number), promotion_id)
    if (
        not pr.get("merged")
        or _require_sha(str(pr.get("merge_commit_sha") or ""), "merged initial PR SHA") != expected_sha
        or metadata.target != deployment_target
        or metadata.deployment_branch != deployment_branch
        or not current
        or current.state not in {LifecycleState.INITIAL_PR_MERGED, LifecycleState.DEPLOYMENT_SUCCEEDED, LifecycleState.COMPLETED}
        or str(actual_result).lower() != "success"
        or _require_sha(actual_sha, "actual deployed SHA") != expected_sha
    ):
        raise RuntimeError("deployment result does not match the authenticated promotion")
    verify_deployment_branch(client, deployment_branch, expected_sha)
    _record_once(
        client,
        number,
        promotion_id,
        LifecycleState.DEPLOYMENT_SUCCEEDED,
        deployment_branch=deployment_branch,
        deployment_sha=expected_sha,
    )
    if deployment_target == "MASTER":
        _record_once(client, number, promotion_id, LifecycleState.COMPLETED, deployment_sha=expected_sha)
        return False, ""
    if deployment_target not in {"PSUP", "PROD"}:
        raise RuntimeError("post-deployment validation is only supported for PSUP and PROD")
    return True, config_mod.load(cfg_path).validation_environment(deployment_target)


def begin_validation(
    client: GhClient,
    promotion_id: str,
    number: int,
    deployment_target: str,
    deployment_branch: str,
    deployed_sha: str,
    validation_environment: str,
    cfg_path: Path,
) -> None:
    metadata, _pr = _managed_pr(client, promotion_id, number)
    deployed_sha = _require_sha(deployed_sha, "deployed SHA")
    if (
        deployment_target not in {"PSUP", "PROD"}
        or metadata.target != deployment_target
        or metadata.deployment_branch != deployment_branch
        or config_mod.load(cfg_path).validation_environment(deployment_target) != validation_environment
    ):
        raise RuntimeError("validation inputs do not match the authenticated promotion")
    current = _latest_record(_comments(client, number), promotion_id)
    if current and current.state == LifecycleState.WAITING_FOR_VALIDATION:
        if str(current.data.get("deployment_sha") or "").lower() != deployed_sha:
            raise RuntimeError("existing validation state has a different deployed SHA")
        return
    if (
        not current
        or current.state != LifecycleState.DEPLOYMENT_SUCCEEDED
        or str(current.data.get("deployment_sha") or "").lower() != deployed_sha
    ):
        raise RuntimeError("validation cannot start before the exact deployment succeeds")
    verify_deployment_branch(client, deployment_branch, deployed_sha)
    _record(
        client,
        number,
        promotion_id,
        LifecycleState.WAITING_FOR_VALIDATION,
        deployment_branch=deployment_branch,
        deployment_sha=deployed_sha,
        validation_environment=validation_environment,
    )


def approve_validation(
    client: GhClient,
    promotion_id: str,
    number: int,
    deployment_target: str,
    deployment_branch: str,
    deployed_sha: str,
) -> None:
    metadata, _pr = _managed_pr(client, promotion_id, number)
    deployed_sha = _require_sha(deployed_sha, "deployed SHA")
    current = _latest_record(_comments(client, number), promotion_id)
    if (
        deployment_target not in {"PSUP", "PROD"}
        or metadata.target != deployment_target
        or metadata.release_branch != deployment_branch
        or not current
        or current.state != LifecycleState.WAITING_FOR_VALIDATION
        or str(current.data.get("deployment_sha") or "").lower() != deployed_sha
    ):
        raise RuntimeError("Environment validation does not match the authenticated deployment")
    verify_deployment_branch(client, deployment_branch, deployed_sha)
    _record_once(client, number, promotion_id, LifecycleState.VALIDATION_APPROVED, deployment_sha=deployed_sha)


def advance_final_pr(
    client: GhClient,
    promotion_id: str,
    number: int,
    deployment_target: str,
    release_branch: str,
    deployed_sha: str,
) -> FinalPrProgress:
    metadata, _pr = _managed_pr(client, promotion_id, number)
    deployed_sha = _require_sha(deployed_sha, "deployed SHA")
    current = _latest_record(_comments(client, number), promotion_id)
    if (
        deployment_target not in {"PSUP", "PROD"}
        or metadata.target != deployment_target
        or metadata.release_branch != release_branch
        or not current
        or current.state not in {LifecycleState.VALIDATION_APPROVED, LifecycleState.FINAL_PR_CREATED}
    ):
        raise RuntimeError("finalization does not follow an approved authenticated validation")
    verify_deployment_branch(client, release_branch, deployed_sha)
    existing = client.command(
        "pr", "list", "--repo", _repo(), "--head", release_branch, "--base", deployment_target,
        "--state", "all", "--json", "number,body,url",
    )
    final_number = ""
    final_url = ""
    for item in json.loads(existing or "[]"):
        if FINAL_MARKER in str(item.get("body") or "") and promotion_id in str(item.get("body") or ""):
            final_number = str(item.get("number") or "")
            final_url = str(item.get("url") or "")
            break
    if not final_number and current.state == LifecycleState.FINAL_PR_CREATED:
        final_number = str(current.data.get("final_pr_number") or "")
        final_url = str(current.data.get("final_pr_url") or "")
    if not final_number:
        body = "\n".join([
            MANAGED_MARKER,
            FINAL_MARKER,
            metadata_comment(metadata),
            f"Initial PR: #{number}",
            "",
            "Validated deployment synchronization.",
        ])
        final_url = client.command(
            "pr", "create", "--repo", _repo(), "--head", release_branch, "--base", deployment_target,
            "--title", f"Finalize {deployment_target} promotion: {promotion_id}", "--body", body,
        )
        match = re.search(r"/pull/(\d+)(?:/)?$", final_url)
        if not match:
            raise RuntimeError("GitHub did not return a Pull Request URL for final synchronization")
        final_number = match.group(1)
    if current.state != LifecycleState.FINAL_PR_CREATED:
        _record_once(
            client, number, promotion_id, LifecycleState.FINAL_PR_CREATED,
            final_pr_number=final_number, final_pr_url=final_url, deployment_sha=deployed_sha,
        )
        # This is a normal protected auto-merge request, never an admin bypass.
        client.command(
            "pr", "merge", final_number, "--repo", _repo(), "--squash", "--auto",
            "--match-head-commit", deployed_sha,
        )
    view_text = client.command(
        "pr", "view", final_number, "--repo", _repo(),
        "--json", "number,url,merged,mergeCommit,state,headRefName,baseRefName,headRefOid",
    )
    view = json.loads(view_text or "{}")
    if (
        str(view.get("headRefName") or "") != release_branch
        or str(view.get("baseRefName") or "") != deployment_target
        or str(view.get("headRefOid") or "").lower() != deployed_sha
    ):
        raise RuntimeError("final Pull Request no longer matches the deployed release branch")
    if view.get("merged"):
        merge_sha = _require_sha(str((view.get("mergeCommit") or {}).get("oid") or ""), "final merge SHA")
        _record_once(client, number, promotion_id, LifecycleState.FINAL_PR_MERGED, final_pr_number=final_number, final_merge_sha=merge_sha)
        _record_once(client, number, promotion_id, LifecycleState.COMPLETED, final_pr_number=final_number, final_merge_sha=merge_sha, release_branch=release_branch)
        return FinalPrProgress("merged", final_number, str(view.get("url") or final_url), merge_sha)
    if str(view.get("state") or "").upper() != "OPEN":
        raise RuntimeError("final Pull Request was closed without merging")
    return FinalPrProgress("waiting", final_number, str(view.get("url") or final_url))


def prepare_rollback(
    client: GhClient,
    promotion_id: str,
    number: int,
    deployment_target: str,
    release_branch: str,
    deployed_sha: str,
    validation_result: str,
    cfg_path: Path,
) -> tuple[str, str]:
    """Record rejected validation and return the target revision for a parent-run rollback."""
    metadata, _pr = _managed_pr(client, promotion_id, number)
    if (
        validation_result == "success"
        or deployment_target not in {"PSUP", "PROD"}
        or metadata.target != deployment_target
        or metadata.release_branch != release_branch
    ):
        raise RuntimeError("rollback request does not match a rejected authenticated validation")
    _require_sha(deployed_sha, "deployed SHA")
    rollback_branch = config_mod.load(cfg_path).resolve(deployment_target).target
    rollback_sha = _branch_sha(client, rollback_branch)
    current = _latest_record(_comments(client, number), promotion_id)
    if current and current.state == LifecycleState.ROLLBACK_TRIGGERED:
        return (
            str(current.data.get("rollback_branch") or rollback_branch),
            _require_sha(str(current.data.get("rollback_sha") or rollback_sha), "rollback SHA"),
        )
    if not current or current.state != LifecycleState.WAITING_FOR_VALIDATION:
        raise RuntimeError("rollback cannot start before the exact validation wait state")
    _record(client, number, promotion_id, LifecycleState.VALIDATION_REJECTED, conclusion=validation_result)
    _record_once(
        client,
        number,
        promotion_id,
        LifecycleState.ROLLBACK_TRIGGERED,
        rollback_branch=rollback_branch,
        rollback_sha=rollback_sha,
        reason="Environment validation was not approved",
    )
    return rollback_branch, rollback_sha


def _write_outputs(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m promotion.lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)

    wait = sub.add_parser("initial-pr-progress")
    wait.add_argument("--promotion-id", required=True)
    wait.add_argument("--initial-pr-number", required=True, type=int)
    wait.add_argument("--initial-pr-head-sha", required=True)
    wait.add_argument("--staging-branch", required=True)
    wait.add_argument("--initial-pr-base", required=True)

    merged = sub.add_parser("verify-initial-merge")
    merged.add_argument("--promotion-id", required=True)
    merged.add_argument("--initial-pr-number", required=True, type=int)
    merged.add_argument("--merged-sha", required=True)
    merged.add_argument("--deployment-branch", required=True)
    merged.add_argument("--deployment-target", required=True)
    merged.add_argument("--deployment-action", required=True)

    branch = sub.add_parser("verify-deployment-branch")
    branch.add_argument("--deployment-branch", required=True)
    branch.add_argument("--deployment-sha", required=True)

    deployed = sub.add_parser("record-deployment-completed")
    deployed.add_argument("--promotion-id", required=True)
    deployed.add_argument("--initial-pr-number", required=True, type=int)
    deployed.add_argument("--deployment-target", required=True)
    deployed.add_argument("--deployment-branch", required=True)
    deployed.add_argument("--expected-deployment-sha", required=True)
    deployed.add_argument("--actual-deployment-result", required=True)
    deployed.add_argument("--actual-deployed-sha", required=True)
    deployed.add_argument("--repo-root", default=".")

    start = sub.add_parser("validation-started")
    start.add_argument("--promotion-id", required=True)
    start.add_argument("--initial-pr-number", required=True, type=int)
    start.add_argument("--deployment-target", required=True)
    start.add_argument("--deployment-branch", required=True)
    start.add_argument("--deployed-sha", required=True)
    start.add_argument("--validation-environment", required=True)
    start.add_argument("--repo-root", default=".")

    approved = sub.add_parser("validation-approved")
    approved.add_argument("--promotion-id", required=True)
    approved.add_argument("--initial-pr-number", required=True, type=int)
    approved.add_argument("--deployment-target", required=True)
    approved.add_argument("--deployment-branch", required=True)
    approved.add_argument("--deployed-sha", required=True)

    final = sub.add_parser("final-pr-progress")
    final.add_argument("--promotion-id", required=True)
    final.add_argument("--initial-pr-number", required=True, type=int)
    final.add_argument("--deployment-target", required=True)
    final.add_argument("--release-branch", required=True)
    final.add_argument("--deployed-sha", required=True)

    failed = sub.add_parser("validation-failed")
    failed.add_argument("--promotion-id", required=True)
    failed.add_argument("--initial-pr-number", required=True, type=int)
    failed.add_argument("--deployment-target", required=True)
    failed.add_argument("--release-branch", required=True)
    failed.add_argument("--deployed-sha", required=True)
    failed.add_argument("--validation-result", required=True)
    failed.add_argument("--repo-root", default=".")

    args = parser.parse_args(argv)
    client = GhCli(_repo())
    if args.command == "initial-pr-progress":
        result = advance_initial_pr(client, args.promotion_id, args.initial_pr_number, args.initial_pr_head_sha, args.staging_branch, args.initial_pr_base)
        _write_outputs(approval_result=result.result, merged_sha=result.merged_sha, merged_branch=result.merged_branch)
        print(result.result)
        return 0 if result.result == "merged" else 3
    if args.command == "verify-initial-merge":
        verify_initial_merge(client, args.promotion_id, args.initial_pr_number, args.merged_sha, args.deployment_branch, args.deployment_target, args.deployment_action)
        _write_outputs(deployment_branch=args.deployment_branch, deployment_sha=args.merged_sha, deployment_target=args.deployment_target, deployment_action=args.deployment_action)
    elif args.command == "verify-deployment-branch":
        verify_deployment_branch(client, args.deployment_branch, args.deployment_sha)
        _write_outputs(deployment_branch=args.deployment_branch, deployed_sha=args.deployment_sha)
    elif args.command == "record-deployment-completed":
        requires_validation, environment = record_deployment_completed(client, args.promotion_id, args.initial_pr_number, args.deployment_target, args.deployment_branch, args.expected_deployment_sha, args.actual_deployment_result, args.actual_deployed_sha, Path(args.repo_root))
        _write_outputs(deployment_verified="true", requires_validation=str(requires_validation).lower(), validation_environment=environment)
    elif args.command == "validation-started":
        begin_validation(client, args.promotion_id, args.initial_pr_number, args.deployment_target, args.deployment_branch, args.deployed_sha, args.validation_environment, Path(args.repo_root))
        _write_outputs(validation_result="waiting", validated_sha="")
    elif args.command == "validation-approved":
        approve_validation(client, args.promotion_id, args.initial_pr_number, args.deployment_target, args.deployment_branch, args.deployed_sha)
        _write_outputs(validation_result="approved", validated_sha=args.deployed_sha)
    elif args.command == "final-pr-progress":
        result = advance_final_pr(client, args.promotion_id, args.initial_pr_number, args.deployment_target, args.release_branch, args.deployed_sha)
        _write_outputs(final_pr_number=result.number, final_pr_url=result.url, final_merge_sha=result.merge_sha, final_result=result.result)
        print(result.result)
        return 0 if result.result == "merged" else 3
    else:
        rollback_branch, rollback_sha = prepare_rollback(client, args.promotion_id, args.initial_pr_number, args.deployment_target, args.release_branch, args.deployed_sha, args.validation_result, Path(args.repo_root))
        _write_outputs(final_result="rejected", rollback_required="true", rollback_branch=rollback_branch, rollback_sha=rollback_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
