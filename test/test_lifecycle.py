"""Tests for the parent-run promotion lifecycle helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from promotion.lifecycle import (
    FINAL_MARKER,
    MANAGED_MARKER,
    LifecycleRecord,
    LifecycleState,
    PromotionMetadata,
    advance_final_pr,
    advance_initial_pr,
    approve_validation,
    begin_validation,
    deployment_action_for,
    metadata_comment,
    parse_metadata,
    prepare_rollback,
    record_deployment_completed,
    state_comment,
    verify_deployment_branch,
    verify_initial_merge,
)


SHA = "d" * 40
OTHER_SHA = "e" * 40
ROLLBACK_SHA = "c" * 40


class FakeGh:
    def __init__(self, pr: dict, reviews: list[dict] | None = None) -> None:
        self.pr = pr
        self.reviews = reviews or []
        self.commands: list[tuple[str, ...]] = []
        self.comments: list[dict[str, str]] = []
        self.refs = {"release/test_psup": SHA, "psup": ROLLBACK_SHA, "master": SHA}
        self.pr_files: list[dict[str, str]] = [{"filename": "workflows/example.json"}]
        self.final_prs: list[dict[str, str]] = []
        self.final_view: dict = {}
        self.created_final_url = "https://example.invalid/owner/repo/pull/99"

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, str] | None = None,
    ):  # type: ignore[no-untyped-def]
        endpoint_path = endpoint.split("?", 1)[0]
        if endpoint_path.endswith("/reviews"):
            return self.reviews
        if endpoint_path.endswith("/comments"):
            if method == "POST":
                assert fields is not None
                self.comments.append(fields)
                return {"id": len(self.comments)}
            return self.comments
        if "/git/ref/heads/" in endpoint_path:
            branch = endpoint_path.split("/git/ref/heads/", 1)[1]
            return {"object": {"sha": self.refs.get(branch, SHA)}}
        if "/pulls/" in endpoint_path:
            return self.pr
        raise AssertionError(endpoint)

    def api_all(self, endpoint: str) -> list[dict[str, str]]:
        if endpoint.split("?", 1)[0].endswith("/files"):
            return self.pr_files
        raise AssertionError(endpoint)

    def command(self, *args: str) -> str:
        self.commands.append(args)
        if args[:2] == ("pr", "list"):
            return json.dumps(self.final_prs)
        if args[:2] == ("pr", "create"):
            self.final_prs = [{
                "number": "99",
                "body": f"{FINAL_MARKER}\nrun-123",
                "url": self.created_final_url,
            }]
            return self.created_final_url
        if args[:2] == ("pr", "view"):
            return json.dumps(self.final_view)
        return ""


def _metadata(target: str = "PSUP") -> PromotionMetadata:
    release = "release/test_psup" if target == "PSUP" else None
    branch = release or "master"
    return PromotionMetadata(
        promotion_id="run-123",
        target=target,
        staging_branch="staging/test",
        release_branch=release,
        deployment_branch=branch,
        deployment_action="create/update_workflow",
        has_workflow_changes=True,
        initial_pr_base=branch,
        base_sha="a" * 40,
    )


def _pr(metadata: PromotionMetadata | None = None, *, merged: bool = False) -> dict:
    metadata = metadata or _metadata()
    return {
        "number": 41,
        "body": f"{MANAGED_MARKER}\n{metadata_comment(metadata)}",
        "merged": merged,
        "merge_commit_sha": SHA if merged else None,
        "state": "closed" if merged else "open",
        "draft": False,
        "head": {"ref": metadata.staging_branch, "sha": "b" * 40},
        "base": {"ref": metadata.initial_pr_base},
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
                    "validation_environments": {"PSUP": "ReleaseApproval", "PROD": "ReleaseApproval"},
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _state(state: LifecycleState, data: dict | None = None) -> dict[str, str]:
    return {
        "body": state_comment(
            LifecycleRecord(
                promotion_id="run-123",
                state=state,
                recorded_at="2026-09-03T00:00:00Z",
                data=data or {"deployment_sha": SHA},
            )
        )
    }


def _setup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")


def test_workflow_change_action_selection() -> None:
    assert deployment_action_for(True) == "create/update_workflow"
    assert deployment_action_for(False) == "create/update_repo"


def test_unsigned_metadata_is_accepted() -> None:
    parsed = parse_metadata(f"{MANAGED_MARKER}\n{metadata_comment(_metadata())}")
    assert parsed is not None
    assert parsed.promotion_id == "run-123"


def test_legacy_signature_field_is_ignored() -> None:
    payload = {**_metadata().__dict__, "signature": "legacy-signature"}
    body = f"{MANAGED_MARKER}\n<!-- dbx-promotion-metadata: {json.dumps(payload)} -->"
    parsed = parse_metadata(body)
    assert parsed is not None
    assert parsed.promotion_id == "run-123"


def test_initial_pr_waits_for_mandatory_manual_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(), [])

    result = advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")

    assert result.result == "waiting"
    assert gh.commands == []
    assert "WAITING_FOR_PR_APPROVAL" in gh.comments[-1]["body"]


def test_initial_pr_never_auto_merges_even_with_review(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(), [{"state": "APPROVED", "user": {"login": "reviewer"}}])

    first = advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")
    second = advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")

    assert first.result == second.result == "waiting"
    assert gh.commands == []
    assert len(gh.comments) == 1


def test_initial_pr_reports_actual_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True), [])

    result = advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")

    assert result.result == "merged"
    assert result.merged_sha == SHA
    assert result.merged_branch == "release/test_psup"
    assert "INITIAL_PR_APPROVED" in gh.comments[-1]["body"]
    assert '"approval_method": "manual_merge"' in gh.comments[-1]["body"]


def test_changed_staging_head_is_allowed_before_manual_merge(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    pr = _pr()
    pr["head"]["sha"] = OTHER_SHA
    gh = FakeGh(pr, [])

    result = advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")

    assert result.result == "waiting"


def test_closed_initial_pr_without_merge_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    pr = _pr()
    pr["state"] = "closed"
    gh = FakeGh(pr)

    with pytest.raises(RuntimeError, match="closed without merging"):
        advance_initial_pr(gh, "run-123", 41, "staging/test", "release/test_psup")


def test_initial_merge_recalculates_workflow_deployment_action(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.comments = [_state(LifecycleState.INITIAL_PR_APPROVED)]

    action = verify_initial_merge(
        gh, "run-123", 41, SHA, "release/test_psup", "PSUP", _config(tmp_path)
    )

    assert action == "create/update_workflow"
    assert "INITIAL_PR_MERGED" in gh.comments[-1]["body"]


def test_initial_merge_recalculates_repo_deployment_action(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.pr_files = [{"filename": "Notebooks/application.py"}]
    gh.comments = [_state(LifecycleState.INITIAL_PR_APPROVED)]

    action = verify_initial_merge(
        gh, "run-123", 41, SHA, "release/test_psup", "PSUP", _config(tmp_path)
    )

    assert action == "create/update_repo"


def test_workflow_rename_in_final_pr_selects_workflow_action(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.pr_files = [{"filename": "Notebooks/renamed.json", "previous_filename": "workflows/job.json"}]
    gh.comments = [_state(LifecycleState.INITIAL_PR_APPROVED)]

    action = verify_initial_merge(
        gh, "run-123", 41, SHA, "release/test_psup", "PSUP", _config(tmp_path)
    )

    assert action == "create/update_workflow"


def test_initial_merge_sha_mismatch_fails(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.comments = [_state(LifecycleState.INITIAL_PR_APPROVED)]

    with pytest.raises(RuntimeError, match="merge does not match"):
        verify_initial_merge(
            gh, "run-123", 41, OTHER_SHA, "release/test_psup", "PSUP", _config(tmp_path)
        )


def test_deployment_branch_is_checked_before_use(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = SHA
    verify_deployment_branch(gh, "release/test_psup", SHA)
    gh.refs["release/test_psup"] = OTHER_SHA
    with pytest.raises(RuntimeError, match="branch HEAD"):
        verify_deployment_branch(gh, "release/test_psup", SHA)


def test_master_deployment_completes_without_validation(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    metadata = _metadata("MASTER")
    gh = FakeGh(_pr(metadata, merged=True))
    gh.comments = [_state(LifecycleState.INITIAL_PR_MERGED)]

    result = record_deployment_completed(
        gh, "run-123", 41, "MASTER", "master", SHA, "success", SHA, _config(tmp_path)
    )

    assert result == (False, "")
    assert "COMPLETED" in gh.comments[-1]["body"]


def test_psup_deployment_requires_environment_validation(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.comments = [_state(LifecycleState.INITIAL_PR_MERGED)]

    result = record_deployment_completed(
        gh, "run-123", 41, "PSUP", "release/test_psup", SHA, "success", SHA, _config(tmp_path)
    )

    assert result == (True, "ReleaseApproval")
    assert "DEPLOYMENT_SUCCEEDED" in gh.comments[-1]["body"]


def test_failed_deployment_is_rejected(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr(merged=True))
    gh.comments = [_state(LifecycleState.INITIAL_PR_MERGED)]

    with pytest.raises(RuntimeError, match="deployment result"):
        record_deployment_completed(
            gh, "run-123", 41, "PSUP", "release/test_psup", SHA, "failure", SHA, _config(tmp_path)
        )


def test_validation_records_waiting_state_without_expiry(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = SHA
    gh.comments = [_state(LifecycleState.DEPLOYMENT_SUCCEEDED)]

    begin_validation(gh, "run-123", 41, "PSUP", "release/test_psup", SHA, "ReleaseApproval", _config(tmp_path))

    body = gh.comments[-1]["body"]
    assert "WAITING_FOR_VALIDATION" in body
    assert "expires_at" not in body


def test_validation_approval_checks_release_head(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = SHA
    gh.comments = [_state(LifecycleState.WAITING_FOR_VALIDATION)]

    approve_validation(gh, "run-123", 41, "PSUP", "release/test_psup", SHA)

    assert "VALIDATION_APPROVED" in gh.comments[-1]["body"]


def test_release_movement_blocks_validation_approval(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = OTHER_SHA
    gh.comments = [_state(LifecycleState.WAITING_FOR_VALIDATION)]

    with pytest.raises(RuntimeError, match="branch HEAD"):
        approve_validation(gh, "run-123", 41, "PSUP", "release/test_psup", SHA)


def test_final_pr_is_created_and_pinned_to_deployed_sha(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = SHA
    gh.comments = [_state(LifecycleState.VALIDATION_APPROVED)]
    gh.final_view = {
        "number": 99,
        "url": gh.created_final_url,
        "merged": False,
        "state": "OPEN",
        "headRefName": "release/test_psup",
        "baseRefName": "PSUP",
        "headRefOid": SHA,
    }

    result = advance_final_pr(gh, "run-123", 41, "PSUP", "release/test_psup", SHA)

    assert result.result == "waiting"
    assert any(command[:2] == ("pr", "create") for command in gh.commands)
    merge = next(command for command in gh.commands if command[:2] == ("pr", "merge"))
    assert merge[-2:] == ("--match-head-commit", SHA)
    assert "--admin" not in merge


def test_final_pr_merge_is_detected_without_creating_duplicate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["release/test_psup"] = SHA
    gh.comments = [_state(LifecycleState.FINAL_PR_CREATED, {"deployment_sha": SHA, "final_pr_number": "99", "final_pr_url": gh.created_final_url})]
    gh.final_prs = [{"number": "99", "body": f"{FINAL_MARKER}\nrun-123", "url": gh.created_final_url}]
    gh.final_view = {
        "number": 99,
        "url": gh.created_final_url,
        "merged": True,
        "mergeCommit": {"oid": OTHER_SHA},
        "state": "CLOSED",
        "headRefName": "release/test_psup",
        "baseRefName": "PSUP",
        "headRefOid": SHA,
    }

    result = advance_final_pr(gh, "run-123", 41, "PSUP", "release/test_psup", SHA)

    assert result.result == "merged"
    assert len([command for command in gh.commands if command[:2] == ("pr", "create")]) == 0
    assert result.merge_sha == OTHER_SHA


def test_rejected_validation_prepares_parent_run_rollback(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _setup(monkeypatch)
    gh = FakeGh(_pr())
    gh.refs["psup"] = ROLLBACK_SHA
    gh.comments = [_state(LifecycleState.WAITING_FOR_VALIDATION)]

    branch, sha = prepare_rollback(
        gh, "run-123", 41, "PSUP", "release/test_psup", SHA, "failure", _config(tmp_path)
    )

    assert (branch, sha) == ("psup", ROLLBACK_SHA)
    assert "ROLLBACK_TRIGGERED" in gh.comments[-1]["body"]
    assert all("workflow" not in command for command in gh.commands)


def test_reusable_workflow_interfaces_and_parent_graph() -> None:
    workflow_dir = Path(".github/workflows")
    parent = (workflow_dir / "code_promotion.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in parent
    for child in (
        "promotion_pr_approved.yml",
        "promotion_initial_merged.yml",
        "trigger_DBX_WF_management.yaml",
        "promotion_deployment_completed.yml",
        "promotion_deployment_validation.yml",
        "promotion_validation_completed.yml",
    ):
        text = (workflow_dir / child).read_text(encoding="utf-8")
        assert "workflow_call:" in text
        if child != "trigger_DBX_WF_management.yaml":
            assert "workflow_dispatch:" not in text
        assert "workflow_run:" not in text
        assert "pull_request_review:" not in text
        assert "pull_request:" not in text
    assert "uses: ./.github/workflows/promotion_pr_approved.yml" in parent
    assert "uses: ./.github/workflows/promotion_initial_merged.yml" in parent
    assert "uses: ./.github/workflows/trigger_DBX_WF_management.yaml" in parent
    assert "uses: ./.github/workflows/promotion_deployment_completed.yml" in parent
    assert "uses: ./.github/workflows/promotion_deployment_validation.yml" in parent
    assert "uses: ./.github/workflows/promotion_validation_completed.yml" in parent
    assert parent.count("secrets: inherit") == 7
    expected_outputs = {
        "promotion_pr_approved.yml": ("merged_sha", "merged_branch", "approval_result"),
        "promotion_initial_merged.yml": ("deployment_sha", "deployment_branch", "deployment_action"),
        "trigger_DBX_WF_management.yaml": ("deployment_result", "deployed_sha", "deployment_branch"),
        "promotion_deployment_completed.yml": ("deployment_verified", "requires_validation", "validation_environment"),
        "promotion_deployment_validation.yml": ("validation_result", "validated_sha"),
        "promotion_validation_completed.yml": ("final_pr_number", "final_pr_url", "final_merge_sha", "final_result"),
    }
    for child, outputs in expected_outputs.items():
        child_text = (workflow_dir / child).read_text(encoding="utf-8")
        for output in outputs:
            assert f"      {output}:" in child_text


def test_no_internal_workflow_dispatch_or_old_event_chain() -> None:
    paths = (
        list(Path(".github/workflows").glob("*"))
        + list(Path("office_workflow_templates").glob("*"))
        + [Path("promotion/lifecycle.py")]
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "gh workflow run" not in text
    assert "repository_dispatch" not in text
    assert "workflow_run:" not in text
    assert "pull_request_review:" not in text
    assert "pull_request:" not in text


def test_enterprise_templates_match_reusable_contract() -> None:
    template_dir = Path("office_workflow_templates")
    parent = (template_dir / "code_promotion.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in parent
    for child in (
        "promotion_pr_approved.yml",
        "promotion_initial_merged.yml",
        "code_promotion_dbx_management.yml",
        "promotion_deployment_completed.yml",
        "promotion_deployment_validation.yml",
        "promotion_validation_completed.yml",
    ):
        child_text = (template_dir / child).read_text(encoding="utf-8")
        assert "workflow_call:" in child_text
        assert "runs-on: self-hosted" in child_text
        assert "workflow_dispatch:" not in child_text
    assert "environment:\n      name: ${{ inputs.validation_environment }}" in (
        template_dir / "promotion_deployment_validation.yml"
    ).read_text(encoding="utf-8")
    assert parent.count("secrets: inherit") == 7
    assert "Validate repository token" in parent
    assert "GH_ENTERPRISE_TOKEN=\"$REPO_TOKEN\" gh api --hostname github.kp.org" in parent
    assert "PROMOTION_LIFECYCLE_HMAC_KEY" not in parent


def test_deployment_uses_same_environment_concurrency() -> None:
    text = Path(".github/workflows/trigger_DBX_WF_management.yaml").read_text(encoding="utf-8")
    assert "group: dbx-deployment-${{ inputs.environment }}" in text
    assert "workflow_dispatch:" in text
    assert "workflow_call:" in text
    assert "      action:" in text
    assert "deployment_result=success" in text
    assert "No DBX, ServiceNow, or organization resource was changed." in text
    assert "execute_dbx_wf_management.yml" not in text


def test_code_promotion_fails_before_mutation_when_repo_token_is_missing() -> None:
    parent = Path(".github/workflows/code_promotion.yml").read_text(encoding="utf-8")
    preflight = parent.index("- name: Validate repository token")
    checkout = parent.index("- name: Check out trusted promotion automation")
    promote = parent.index("name: Prepare promotion and open initial Pull Request")
    assert preflight < checkout < promote
    assert "title=Missing REPO_TOKEN" in parent
    assert "PROMOTION_LIFECYCLE_HMAC_KEY" not in parent
    assert "GH_TOKEN=\"$REPO_TOKEN\" gh api \"repos/${GITHUB_REPOSITORY}\"" in parent


def test_office_deployment_template_keeps_org_integrations() -> None:
    template = Path("office_workflow_templates/trigger_DBX_WF_management.yaml").read_text(encoding="utf-8")
    adapter = Path("office_workflow_templates/code_promotion_dbx_management.yml").read_text(encoding="utf-8")
    parent = Path("office_workflow_templates/code_promotion.yml").read_text(encoding="utf-8")
    assert "          - uat" in template
    assert "          - master" not in template
    assert "workflow_call:" not in template
    assert template.count("repo_ref: ${{ github.ref }}") == 2
    assert "outputs:" not in template
    assert "automation-result:" not in template
    assert "uses: ./.github/workflows/code_promotion_dbx_management.yml" in parent
    assert "uses: ./.github/workflows/trigger_DBX_WF_management.yaml" not in parent
    assert "== 'MASTER' && 'uat'" in parent
    assert "== 'PSUP' && 'psup'" in parent
    assert "actual_deployment_result: ${{ needs.deploy.outputs.deployment_result }}" in parent
    assert "actual_deployed_sha: ${{ needs.deploy.outputs.deployed_sha }}" in parent
    assert "run-name: ${{ github.event.pull_request.title }} ${{ inputs.operation }} on ${{ github.ref }}" in template
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in template
    assert "needs: [sn-init, Execute-DBX-WF-Management-psup]" in template
    assert template.count("secrets: inherit") == 6
    assert "verify-deployment-branch" in adapter
    assert adapter.count("repo_ref: ${{ format('refs/heads/{0}', inputs.deployment_branch) }}") == 2
    assert "execute_dbx_wf_management.yml@master" in adapter
    assert "service_now_asr_creation_and_validation.yml@master" in adapter
    assert "service_now_asr_closure.yml@master" in adapter


def test_obsolete_timeout_workflow_is_removed() -> None:
    assert not Path(".github/workflows/promotion_validation_timeout.yml").exists()
