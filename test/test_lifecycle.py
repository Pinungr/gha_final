"""Unit tests for the event-driven promotion lifecycle policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from promotion.lifecycle import (
    MANAGED_MARKER,
    LifecycleState,
    PromotionMetadata,
    deployment_action_for,
    handle_initial_approval,
    metadata_comment,
    metadata_is_authenticated,
    parse_metadata,
    sign_metadata,
    validation_is_expired,
)


class FakeGh:
    def __init__(self, pr: dict, reviews: list[dict]) -> None:
        self.pr = pr
        self.reviews = reviews
        self.commands: list[tuple[str, ...]] = []
        self.comments: list[dict[str, str]] = []

    def api(self, endpoint: str, *, method: str = "GET", fields: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
        if endpoint.endswith("/reviews"):
            return self.reviews
        if endpoint.endswith("/comments") and method == "POST":
            assert fields is not None
            self.comments.append(fields)
            return {"id": len(self.comments)}
        if "/pulls/" in endpoint:
            return self.pr
        raise AssertionError(endpoint)

    def command(self, *args: str) -> str:
        self.commands.append(args)
        return ""

    def api_all(self, endpoint: str):  # type: ignore[no-untyped-def]
        raise AssertionError(endpoint)


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
