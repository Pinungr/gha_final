"""Unit tests for the event-driven promotion lifecycle policy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from promotion.lifecycle import (
    MANAGED_MARKER,
    LifecycleState,
    LifecycleRecord,
    PromotionMetadata,
    deployment_action_for,
    handle_deployment_completed,
    handle_initial_approval,
    handle_validation_approved,
    handle_validation_started,
    metadata_comment,
    metadata_is_authenticated,
    parse_metadata,
    sign_metadata,
    state_comment,
    validation_is_expired,
)


class FakeGh:
    def __init__(self, pr: dict, reviews: list[dict]) -> None:
        self.pr = pr
        self.reviews = reviews
        self.commands: list[tuple[str, ...]] = []
        self.comments: list[dict[str, str]] = []

    def api(self, endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        endpoint_path = endpoint.split("?", 1)[0]
        if endpoint_path.endswith("/reviews"):
            return self.reviews
        if endpoint_path.endswith("/comments"):
            if method == "POST":
                assert fields is not None
                self.comments.append(fields)
                return {"id": len(self.comments)}
            return self.comments
        if "/pulls/" in endpoint:
            return self.pr
        raise AssertionError(endpoint)

    def command(self, *args: str) -> str:
        self.commands.append(args)
        return ""

    def api_all(self, endpoint: str):  # type: ignore[no-untyped-def]
        raise AssertionError(endpoint)


class DeploymentGh(FakeGh):
    def api(self, endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        if endpoint == "search/issues":
            return {"items": [{"number": 41}]}
        return super().api(endpoint, method=method, fields=fields)


def _metadata() -> PromotionMetadata:
    return PromotionMetadata(
        promotion_id="run-123",
        target="PSUP",
        staging_branch="staging/test",
        release_branch="release/test_psup",
        deployment_branch="release/test_psup",
        deployment_action="create/update_workflow",
        has_workflow_changes=True,
        initial_pr_base="release/test_psup",
        base_sha="a" * 40,
    )


def _pr(body: str) -> dict:
    return {
        "number": 41,
        "body": body,
        "merged": False,
        "draft": False,
        "head": {"ref": "staging/test", "sha": "b" * 40},
        "base": {"ref": "release/test_psup"},
        "user": {"login": "author"},
    }


def _config(root: Path) -> Path:
    root.joinpath("promotion.config.json").write_text(
        json.dumps(
            {
                "environments": {
                    "MASTER": {"source": "dev_collaboration", "target": "master", "create_release_branch": False},
                    "PSUP": {"source": "master", "target": "psup"},
                    "PROD": {"source": "psup", "target": "prod"},
                },
                "protected_branches": ["dev_collaboration", "master", "psup", "prod"],
                "workflow_path_pattern": "workflows/**",
                "workflows_list_file": "workflows_list.txt",
                "lifecycle": {
                    "validation_environments": {
                        "MASTER": "ReleaseApproval",
                        "PSUP": "ReleaseApproval",
                        "PROD": "ReleaseApproval",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _state(state: LifecycleState, deployment_sha: str = "d" * 40) -> dict[str, str]:
    return {
        "body": state_comment(
            LifecycleRecord(
                promotion_id="run-123",
                state=state,
                recorded_at="2026-09-03T00:00:00Z",
                data={"deployment_sha": deployment_sha},
            )
        )
    }


def test_workflow_change_action_selection() -> None:
    assert deployment_action_for(True) == "create/update_workflow"
    assert deployment_action_for(False) == "create/update_repo"


def test_signed_metadata_cannot_be_forged() -> None:
    signed = sign_metadata(_metadata(), "secret")
    body = f"{MANAGED_MARKER}\n{metadata_comment(signed)}"
    parsed = parse_metadata(body)
    assert parsed is not None
    assert metadata_is_authenticated(parsed, "secret")
    assert not metadata_is_authenticated(parsed, "different-secret")


def test_exact_validation_deadline_is_expired() -> None:
    started = datetime(2026, 9, 3, tzinfo=timezone.utc)
    deadline = started + timedelta(hours=72)
    assert not validation_is_expired(deadline - timedelta(seconds=1), deadline)
    assert validation_is_expired(deadline, deadline)


def test_zero_approval_never_requests_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])

    handle_initial_approval(gh, {"pull_request": gh.pr, "review": {"state": "approved"}})

    assert gh.commands == []
    assert gh.comments == []


def test_one_non_author_approval_requests_protected_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(
        _pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"),
        [{"state": "APPROVED", "user": {"login": "reviewer"}}],
    )

    handle_initial_approval(gh, {"pull_request": gh.pr, "review": {"state": "approved"}})

    assert len(gh.comments) == 1
    assert gh.commands == [
        ("pr", "merge", "41", "--repo", "owner/repo", "--squash", "--auto", "--match-head-commit", "b" * 40)
    ]


def test_unmanaged_pr_event_is_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    gh = FakeGh(_pr("ordinary PR"), [{"state": "APPROVED", "user": {"login": "reviewer"}}])

    handle_initial_approval(gh, {"pull_request": gh.pr, "review": {"state": "approved"}})

    assert gh.commands == []


@pytest.mark.parametrize(
    ("state", "sha"),
    [
        (LifecycleState.INITIAL_PR_MERGED, "d" * 40),
        (LifecycleState.DEPLOYMENT_FAILED, "d" * 40),
        (LifecycleState.DEPLOYMENT_SUCCEEDED, ""),
    ],
)
def test_validation_cannot_start_without_successful_deployment(
    monkeypatch, tmp_path: Path, state: LifecycleState, sha: str
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])
    gh.comments = [_state(state, sha)]

    with pytest.raises(RuntimeError, match="DEPLOYMENT_SUCCEEDED"):
        handle_validation_started(
            gh, "run-123", 41, "PSUP", "500", "https://example.invalid/run/500", _config(tmp_path)
        )

    assert len(gh.comments) == 1
    assert gh.commands == []


def test_successful_deployment_starts_validation_once(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])
    gh.comments = [_state(LifecycleState.DEPLOYMENT_SUCCEEDED)]

    environment = handle_validation_started(
        gh, "run-123", 41, "PSUP", "500", "https://example.invalid/run/500", _config(tmp_path)
    )

    assert environment == "ReleaseApproval"
    assert "WAITING_FOR_VALIDATION" in gh.comments[-1]["body"]


def test_final_merge_is_pinned_to_deployed_sha(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    deployed_sha = "d" * 40
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    gh.comments = [{"body": state_comment(LifecycleRecord("run-123", LifecycleState.WAITING_FOR_VALIDATION, "2026-09-03T00:00:00Z", {"expires_at": expires, "deployment_sha": deployed_sha}))}]

    original_api = gh.api
    def api(endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        if "/git/ref/heads/" in endpoint:
            return {"object": {"sha": deployed_sha}}
        return original_api(endpoint, method=method, fields=fields)
    gh.api = api  # type: ignore[method-assign]
    def command(*args: str) -> str:
        gh.commands.append(args)
        if args[:2] == ("pr", "list"):
            return "[]"
        if args[:2] == ("pr", "create"):
            return "https://example.invalid/owner/repo/pull/99"
        return ""
    gh.command = command  # type: ignore[method-assign]

    handle_validation_approved(gh, "run-123", 41, Path("."))

    merge = next(args for args in gh.commands if args[:2] == ("pr", "merge"))
    assert merge[-2:] == ("--match-head-commit", deployed_sha)


def test_changed_release_branch_blocks_final_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = FakeGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    gh.comments = [{"body": state_comment(LifecycleRecord("run-123", LifecycleState.WAITING_FOR_VALIDATION, "2026-09-03T00:00:00Z", {"expires_at": expires, "deployment_sha": "d" * 40}))}]
    original_api = gh.api
    def api(endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        if "/git/ref/heads/" in endpoint:
            return {"object": {"sha": "e" * 40}}
        return original_api(endpoint, method=method, fields=fields)
    gh.api = api  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="release branch HEAD"):
        handle_validation_approved(gh, "run-123", 41, Path("."))

    assert gh.commands == []
    assert all("VALIDATION_APPROVED" not in item["body"] for item in gh.comments[1:])


def test_promotion_workflow_serializes_preparation_within_target() -> None:
    workflow = Path(".github/workflows/code_promotion.yml").read_text(encoding="utf-8")
    assert "group: code-promotion-${{ inputs.deployment_target }}" in workflow
    assert "cancel-in-progress: false" in workflow


@pytest.mark.parametrize(
    ("branch", "sha", "expected_success"),
    [
        ("release/test_psup", "d" * 40, True),
        ("release/wrong_psup", "d" * 40, False),
        ("release/test_psup", "e" * 40, False),
    ],
)
def test_deployment_completion_requires_expected_branch_and_sha(
    monkeypatch, branch: str, sha: str, expected_success: bool
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PROMOTION_LIFECYCLE_HMAC_KEY", "secret")
    signed = sign_metadata(_metadata(), "secret")
    gh = DeploymentGh(_pr(f"{MANAGED_MARKER}\n{metadata_comment(signed)}"), [])
    gh.comments = [_state(LifecycleState.DEPLOYMENT_TRIGGERED)]
    event = {
        "workflow_run": {
            "display_title": "DBX deployment: run-123",
            "head_branch": branch,
            "head_sha": sha,
            "conclusion": "success",
            "id": 77,
            "html_url": "https://example.invalid/run/77",
        }
    }

    handle_deployment_completed(gh, event)

    assert ("DEPLOYMENT_SUCCEEDED" in gh.comments[-1]["body"]) is expected_success
    assert bool(gh.commands) is expected_success
