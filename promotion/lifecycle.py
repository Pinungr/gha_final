"""Event-driven promotion lifecycle helpers.

The promotion engine only prepares the initial Pull Request.  This module owns
the state carried across later GitHub Actions runs: approval, deployment,
Environment validation, expiry, and (for PSUP/PROD) final synchronization.
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
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from . import config as config_mod

MANAGED_MARKER = "<!-- dbx-promotion-managed -->"
FINAL_MARKER = "<!-- dbx-promotion-final-sync -->"
_METADATA_RE = re.compile(r"<!-- dbx-promotion-metadata: (?P<json>.+?) -->")
_STATE_RE = re.compile(r"<!-- dbx-promotion-state: (?P<json>.+?) -->")
_PROMOTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
    VALIDATION_EXPIRED = "VALIDATION_EXPIRED"
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
    LifecycleState.VALIDATION_EXPIRED,
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


def validation_expiry(started_at: datetime, hours: int) -> datetime:
    return started_at + timedelta(hours=hours)


def validation_is_expired(now: datetime, expires_at: datetime) -> bool:
    """The deadline is exclusive: it is expired at the configured deadline."""
    return now >= expires_at


def can_finalize(state: LifecycleState, now: datetime, expires_at: datetime) -> bool:
    return state == LifecycleState.WAITING_FOR_VALIDATION and not validation_is_expired(now, expires_at)


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


def _event(path: str | None) -> dict[str, Any]:
    if not path:
        raise RuntimeError("GITHUB_EVENT_PATH is required")
    return json.loads(Path(path).read_text(encoding="utf-8"))


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


def _valid_approved_review(reviews: list[dict[str, Any]], author: str) -> bool:
    """Count a non-author reviewer only when their latest review is APPROVED."""
    latest: dict[str, dict[str, Any]] = {}
    for review in reviews:
        login = str((review.get("user") or {}).get("login") or "")
        if login:
            latest[login] = review
    return any(
        login != author and str(review.get("state", "")).upper() == "APPROVED"
        for login, review in latest.items()
    )


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


def handle_initial_approval(client: GhClient, event: dict[str, Any]) -> None:
    pr = event.get("pull_request") or {}
    metadata = _metadata_from_pr(pr)
    if not metadata or str((event.get("review") or {}).get("state", "")).upper() != "APPROVED":
        return
    number = int(pr["number"])
    reviews = client.api(f"repos/{_repo()}/pulls/{number}/reviews") or []
    author = str((pr.get("user") or {}).get("login") or "")
    if not _valid_approved_review(reviews, author):
        return
    current = client.api(f"repos/{_repo()}/pulls/{number}")
    if current.get("merged") or current.get("draft") or current.get("head", {}).get("ref") != metadata.staging_branch or current.get("base", {}).get("ref") != metadata.initial_pr_base:
        return
    _record(client, number, metadata.promotion_id, LifecycleState.INITIAL_PR_APPROVED)
    # No --admin and no branch deletion: GitHub protection/rulesets remain authoritative.
    client.command("pr", "merge", str(number), "--repo", _repo(), "--squash", "--auto", "--match-head-commit", current["head"]["sha"])


def handle_initial_merged(client: GhClient, event: dict[str, Any]) -> None:
    pr = event.get("pull_request") or {}
    metadata = _metadata_from_pr(pr)
    if not metadata or not pr.get("merged"):
        return
    number = int(pr["number"])
    comments = _comments(client, number)
    existing = _latest_record(comments, metadata.promotion_id)
    if existing and existing.state in {LifecycleState.DEPLOYMENT_TRIGGERED, LifecycleState.DEPLOYMENT_SUCCEEDED, LifecycleState.WAITING_FOR_VALIDATION, LifecycleState.COMPLETED}:
        return
    deployed_sha = str((pr.get("merge_commit_sha") or ""))
    _record(client, number, metadata.promotion_id, LifecycleState.INITIAL_PR_MERGED, deployment_sha=deployed_sha)
    client.command("workflow", "run", "trigger_DBX_WF_management.yaml", "--repo", _repo(), "--ref", metadata.deployment_branch,
                   "-f", f"environment={metadata.target}", "-f", f"deployment_action={metadata.deployment_action}",
                   "-f", f"promotion_id={metadata.promotion_id}", "-f", f"initial_pr_number={number}")
    _record(client, number, metadata.promotion_id, LifecycleState.DEPLOYMENT_TRIGGERED, deployment_branch=metadata.deployment_branch, deployment_sha=deployed_sha)


def _find_pr_by_id(client: GhClient, promotion_id: str) -> dict[str, Any] | None:
    result = client.api("search/issues", fields={"q": f"repo:{_repo()} is:pr in:body {promotion_id}"}) or {}
    for item in result.get("items", []):
        pr = client.api(f"repos/{_repo()}/pulls/{item['number']}")
        metadata = _metadata_from_pr(pr)
        if metadata and metadata.promotion_id == promotion_id:
            return pr
    return None


def _request_rollback(
    client: GhClient,
    metadata: PromotionMetadata,
    number: int,
    cfg: config_mod.Config,
    reason: str,
) -> None:
    """Redeploy the current PSUP/PROD target revision after failed validation."""
    if metadata.target not in {"PSUP", "PROD"}:
        return
    rollback_branch = cfg.resolve(metadata.target).target
    ref = client.api(f"repos/{_repo()}/git/ref/heads/{rollback_branch}") or {}
    rollback_sha = str(ref.get("object", {}).get("sha") or "")
    if not rollback_sha:
        raise RuntimeError(f"Cannot roll back {metadata.target}: target branch {rollback_branch!r} has no commit SHA")
    _record(
        client,
        number,
        metadata.promotion_id,
        LifecycleState.ROLLBACK_TRIGGERED,
        rollback_branch=rollback_branch,
        rollback_sha=rollback_sha,
        reason=reason,
    )
    try:
        client.command(
            "workflow",
            "run",
            cfg.deployment_workflow,
            "--repo",
            _repo(),
            "--ref",
            rollback_branch,
            "-f",
            f"environment={metadata.target}",
            "-f",
            "deployment_action=create/update_repo",
            "-f",
            f"promotion_id={metadata.promotion_id}",
            "-f",
            f"initial_pr_number={number}",
        )
    except RuntimeError:
        _record(client, number, metadata.promotion_id, LifecycleState.ROLLBACK_FAILED, reason="rollback dispatch failed")
        raise


def handle_deployment_completed(client: GhClient, event: dict[str, Any]) -> None:
    run = event.get("workflow_run") or {}
    title = str(run.get("display_title") or "")
    match = re.fullmatch(r"DBX deployment: (?P<id>[A-Za-z0-9._-]+)", title)
    if not match:
        return
    pr = _find_pr_by_id(client, match.group("id"))
    if not pr:
        return
    metadata = _metadata_from_pr(pr)
    assert metadata is not None
    number = int(pr["number"])
    previous = _latest_record(_comments(client, number), metadata.promotion_id)
    if not previous:
        return
    is_rollback = previous.state == LifecycleState.ROLLBACK_TRIGGERED
    expected_branch = str(
        (previous.data.get("rollback_branch") if is_rollback else metadata.deployment_branch) or ""
    )
    expected_sha = str(
        (previous.data.get("rollback_sha") if is_rollback else previous.data.get("deployment_sha")) or ""
    )
    if (
        previous.state not in {LifecycleState.DEPLOYMENT_TRIGGERED, LifecycleState.ROLLBACK_TRIGGERED}
        or not expected_branch
        or not expected_sha
        or str(run.get("head_branch") or "") != expected_branch
        or str(run.get("head_sha") or "") != expected_sha
    ):
        return
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion != "success":
        state = LifecycleState.ROLLBACK_FAILED if is_rollback else LifecycleState.DEPLOYMENT_FAILED
        _record(client, number, metadata.promotion_id, state, deployment_run_id=str(run.get("id") or ""), conclusion=conclusion)
        return
    if is_rollback:
        _record(client, number, metadata.promotion_id, LifecycleState.ROLLBACK_SUCCEEDED, rollback_run_id=str(run.get("id") or ""), rollback_run_url=str(run.get("html_url") or ""), rollback_sha=expected_sha)
        return
    _record(client, number, metadata.promotion_id, LifecycleState.DEPLOYMENT_SUCCEEDED, deployment_run_id=str(run.get("id") or ""), deployment_run_url=str(run.get("html_url") or ""), deployment_sha=expected_sha)
    if metadata.target == "MASTER":
        _record(client, number, metadata.promotion_id, LifecycleState.COMPLETED, deployment_sha=expected_sha)
        return
    client.command("workflow", "run", "promotion_deployment_validation.yml", "--repo", _repo(), "--ref", "master",
                   "-f", f"promotion_id={metadata.promotion_id}", "-f", f"initial_pr_number={number}", "-f", f"environment={metadata.target}")


def handle_validation_started(client: GhClient, promotion_id: str, number: int, target: str, validation_run_id: str, validation_run_url: str, cfg_path: Path) -> str:
    if target not in {"PSUP", "PROD"}:
        raise RuntimeError("Post-deployment validation is only used for PSUP and PROD promotions")
    pr = client.api(f"repos/{_repo()}/pulls/{number}")
    metadata = _metadata_from_pr(pr)
    if not metadata or metadata.promotion_id != promotion_id or metadata.target != target:
        raise RuntimeError("validation input does not match a managed promotion Pull Request")
    previous = _latest_record(_comments(client, number), promotion_id)
    deployment_sha = str((previous.data if previous else {}).get("deployment_sha") or "")
    if (
        not previous
        or previous.state != LifecycleState.DEPLOYMENT_SUCCEEDED
        or not deployment_sha
    ):
        raise RuntimeError(
            "Validation cannot start until this promotion records "
            "DEPLOYMENT_SUCCEEDED with a non-empty deployment SHA."
        )
    cfg = config_mod.load(cfg_path)
    expiry = validation_expiry(_now(), cfg.validation_timeout_hours)
    _record(client, number, promotion_id, LifecycleState.WAITING_FOR_VALIDATION, expires_at=_iso(expiry), validation_run_id=validation_run_id, validation_run_url=validation_run_url, deployment_sha=deployment_sha)
    return cfg.validation_environment(target)


def handle_validation_approved(client: GhClient, promotion_id: str, number: int, cfg_path: Path) -> None:
    pr = client.api(f"repos/{_repo()}/pulls/{number}")
    metadata = _metadata_from_pr(pr)
    if not metadata or metadata.promotion_id != promotion_id:
        return
    state = _latest_record(_comments(client, number), promotion_id)
    if not state or state.state != LifecycleState.WAITING_FOR_VALIDATION:
        return
    expires = datetime.fromisoformat(str(state.data["expires_at"]).replace("Z", "+00:00"))
    if validation_is_expired(_now(), expires):
        _record(client, number, promotion_id, LifecycleState.VALIDATION_EXPIRED)
        _request_rollback(
            client,
            metadata,
            number,
            config_mod.load(cfg_path),
            "Environment approval arrived after the validation deadline",
        )
        return
    deployed_sha = str(state.data.get("deployment_sha") or "")
    if not deployed_sha:
        raise RuntimeError(
            "Validation cannot be approved because the successful deployment "
            "SHA is missing."
        )
    if metadata.target == "MASTER":
        _record(client, number, promotion_id, LifecycleState.VALIDATION_APPROVED)
        _record(client, number, promotion_id, LifecycleState.COMPLETED)
        return
    release = metadata.release_branch
    if not release:
        raise RuntimeError("PSUP/PROD promotion has no release branch")
    head = client.api(f"repos/{_repo()}/git/ref/heads/{release}")
    if head.get("object", {}).get("sha") != deployed_sha:
        raise RuntimeError("release branch HEAD differs from the approved deployed revision")
    _record(client, number, promotion_id, LifecycleState.VALIDATION_APPROVED)
    final_body = "\n".join([MANAGED_MARKER, FINAL_MARKER, metadata_comment(metadata), f"Initial PR: #{number}", "", "Validated deployment synchronization."])
    existing = client.command("pr", "list", "--repo", _repo(), "--head", release, "--base", metadata.target, "--state", "open", "--json", "number,body,url")
    final_number: int | None = None
    for item in json.loads(existing or "[]"):
        if FINAL_MARKER in str(item.get("body") or "") and promotion_id in str(item.get("body") or ""):
            final_number = int(item["number"])
            break
    if final_number is None:
        created = client.command("pr", "create", "--repo", _repo(), "--head", release, "--base", metadata.target,
                                 "--title", f"Finalize {metadata.target} promotion: {metadata.promotion_id}", "--body", final_body)
        final_number = int(created.rstrip("/").split("/")[-1])
        _record(client, number, promotion_id, LifecycleState.FINAL_PR_CREATED, final_pr_number=str(final_number))
    # The repository must grant this automation identity a narrowly scoped final-sync bypass if rules require it.
    client.command(
        "pr", "merge", str(final_number), "--repo", _repo(), "--squash", "--auto",
        "--match-head-commit", deployed_sha,
    )


def handle_final_merged(client: GhClient, event: dict[str, Any]) -> None:
    pr = event.get("pull_request") or {}
    metadata = parse_final_metadata(str(pr.get("body") or ""))
    if not metadata or not metadata_is_authenticated(metadata, os.environ.get("PROMOTION_LIFECYCLE_HMAC_KEY", "")) or not pr.get("merged"):
        return
    initial = _find_pr_by_id(client, metadata.promotion_id)
    if not initial:
        return
    number = int(initial["number"])
    _record(client, number, metadata.promotion_id, LifecycleState.FINAL_PR_MERGED,
            final_pr_number=str(pr.get("number") or ""))
    _record(client, number, metadata.promotion_id, LifecycleState.COMPLETED,
            final_pr_number=str(pr.get("number") or ""), release_branch=metadata.release_branch)


def handle_validation_completed(
    client: GhClient,
    event: dict[str, Any],
    cfg_path: Path = Path("."),
) -> None:
    """Record rejected PSUP/PROD validation and redeploy the target branch."""
    run = event.get("workflow_run") or {}
    title = str(run.get("display_title") or "")
    match = re.fullmatch(r"Promotion validation: (?P<id>[A-Za-z0-9._-]+)", title)
    if not match:
        return
    pr = _find_pr_by_id(client, match.group("id"))
    if not pr:
        return
    metadata = _metadata_from_pr(pr)
    assert metadata is not None
    number = int(pr["number"])
    current = _latest_record(_comments(client, number), metadata.promotion_id)
    if not current or current.state != LifecycleState.WAITING_FOR_VALIDATION:
        return
    conclusion = str(run.get("conclusion") or "").lower()
    if conclusion in {"cancelled", "failure", "timed_out", "action_required"}:
        _record(client, number, metadata.promotion_id, LifecycleState.VALIDATION_REJECTED,
                validation_run_id=str(run.get("id") or ""), conclusion=conclusion)
        _request_rollback(
            client,
            metadata,
            number,
            config_mod.load(cfg_path),
            "Environment validation was rejected",
        )


def handle_timeout(client: GhClient, cfg_path: Path) -> int:
    """Expire only managed PRs whose validation state has reached its deadline."""
    cfg = config_mod.load(cfg_path)
    prs = client.api_all(f"repos/{_repo()}/pulls?state=all&per_page=100")
    expired = 0
    for pr in prs:
        metadata = _metadata_from_pr(pr)
        if not metadata:
            continue
        number = int(pr["number"])
        state = _latest_record(_comments(client, number), metadata.promotion_id)
        if not state or state.state != LifecycleState.WAITING_FOR_VALIDATION:
            continue
        expires = datetime.fromisoformat(str(state.data["expires_at"]).replace("Z", "+00:00"))
        if not validation_is_expired(_now(), expires):
            continue
        run_id = str(state.data.get("validation_run_id") or "")
        if run_id:
            client.command("run", "cancel", run_id, "--repo", _repo())
        _record(client, number, metadata.promotion_id, LifecycleState.VALIDATION_EXPIRED, reason="No Environment approval within configured validation window")
        _request_rollback(client, metadata, number, cfg, "Environment validation expired")
        expired += 1
    return expired


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m promotion.lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("initial-approval", "initial-merged", "deployment-completed", "validation-completed", "pr-closed"):
        sub.add_parser(name).add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    start = sub.add_parser("validation-started")
    start.add_argument("--promotion-id", required=True)
    start.add_argument("--initial-pr-number", required=True, type=int)
    start.add_argument("--environment", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--run-url", required=True)
    start.add_argument("--repo-root", default=".")
    approved = sub.add_parser("validation-approved")
    approved.add_argument("--promotion-id", required=True)
    approved.add_argument("--initial-pr-number", required=True, type=int)
    approved.add_argument("--repo-root", default=".")
    timeout = sub.add_parser("timeout")
    timeout.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    client = GhCli(_repo())
    if args.command == "initial-approval":
        handle_initial_approval(client, _event(args.event))
    elif args.command == "initial-merged":
        handle_initial_merged(client, _event(args.event))
    elif args.command == "pr-closed":
        event = _event(args.event)
        handle_initial_merged(client, event)
        handle_final_merged(client, event)
    elif args.command == "deployment-completed":
        handle_deployment_completed(client, _event(args.event))
    elif args.command == "validation-completed":
        handle_validation_completed(client, _event(args.event))
    elif args.command == "validation-started":
        environment = handle_validation_started(client, args.promotion_id, args.initial_pr_number, args.environment, args.run_id, args.run_url, Path(args.repo_root))
        print(f"validation_environment={environment}")
    elif args.command == "validation-approved":
        handle_validation_approved(client, args.promotion_id, args.initial_pr_number, Path(args.repo_root))
    else:
        print(f"expired={handle_timeout(client, Path(args.repo_root))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
