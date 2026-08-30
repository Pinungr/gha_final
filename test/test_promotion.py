"""End-to-end tests for user-created staging branch promotions."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from promotion.errors import (
    E_DUP_PATH,
    E_MISSING_SOURCE,
    E_PROMOTION_FILE_MISSING,
    PromotionError,
)
from promotion.gitops import Git
from promotion.pr import RecordingBackend
from promotion.promote import promote


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _remote_sha(remote: Path, branch: str) -> str:
    return _git(remote, "rev-parse", f"refs/heads/{branch}")


def _remote_text(remote: Path, branch: str, path: str) -> str:
    return _git(remote, "show", f"{branch}:{path}")


def _remote_branches(remote: Path) -> set[str]:
    return set(_git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())


def _make_repository(
    tmp_path: Path,
    promotion_text: str | None,
    *,
    prepopulate_temporary_branch: bool = False,
) -> tuple[Path, Path]:
    """Create master -> P2 plus a user-created temporary branch and runner clone."""
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    runner = tmp_path / "runner"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(author))
    _git(author, "config", "user.name", "Promotion test")
    _git(author, "config", "user.email", "promotion-test@example.invalid")

    _write(
        author,
        "promotion.config.json",
        json.dumps(
            {
                "environments": {"P2": {"source": "master", "target": "P2"}},
                "protected_branches": ["master", "P2"],
                "workflow_path_pattern": "workflows/**",
                "workflows_list_file": "workflows_list.txt",
            }
        ),
    )
    _write(author, "file1", "P2 version\n")
    _write(author, "file2", "P2 version\n")
    _write(author, "file3", "P2 version\n")
    _write(author, "workflows/old.json", '{"workflow": "old"}\n')
    _write(author, "workflows_list.txt", "workflows/old.json\n")
    _git(author, "add", ".")
    _git(author, "commit", "-m", "Base P2 content")
    _git(author, "branch", "-M", "master")
    _git(author, "remote", "add", "origin", str(remote))
    _git(author, "push", "-u", "origin", "master")

    _git(author, "checkout", "-b", "P2")
    _git(author, "push", "-u", "origin", "P2")

    _git(author, "checkout", "master")
    _write(author, "file1", "master version\n")
    _write(author, "file2", "master version\n")
    _write(author, "file3", "master version\n")
    _write(author, "workflows/new.json", '{"workflow": "new"}\n')
    _git(author, "add", ".")
    _git(author, "commit", "-m", "Master promotion content")
    _git(author, "push")

    _git(author, "checkout", "-b", "reltest_30_08_2026", "P2")
    if promotion_text is not None:
        _write(author, "promotion.txt", promotion_text)
    if prepopulate_temporary_branch:
        _write(author, "file1", "master version\n")
        _write(author, "file2", "master version\n")
        _write(author, "file3", "master version\n")
    if promotion_text is not None or prepopulate_temporary_branch:
        _git(author, "add", ".")
        _git(author, "commit", "-m", "Prepare temporary promotion branch")
    _git(author, "push", "-u", "origin", "reltest_30_08_2026")

    _git(tmp_path, "clone", "--branch", "reltest_30_08_2026", str(remote), str(runner))
    _git(runner, "config", "user.name", "Promotion test")
    _git(runner, "config", "user.email", "promotion-test@example.invalid")
    return remote, runner


def _run_promotion(runner: Path) -> tuple[object, RecordingBackend]:
    backend = RecordingBackend()
    result = promote(
        repo_root=runner,
        deployment_target="P2",
        staging_branch="reltest_30_08_2026",
        git=Git(runner),
        pr_backend=backend,
        now=datetime(2026, 8, 30, 10, 20, 30, tzinfo=timezone.utc),
    )
    return result, backend


def test_valid_promotion_txt_updates_the_existing_staging_branch_and_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "file1\nfile2\nfile3\n")

    result, backend = _run_promotion(runner)

    assert result.staging_branch == "reltest_30_08_2026"
    assert result.source_branch == "master"
    assert result.release_branch == "release/30_08_2026_10_20_30_p2"
    assert result.pr is not None
    assert result.pr.head == "reltest_30_08_2026"
    assert result.pr.base == "release/30_08_2026_10_20_30_p2"
    assert len(backend.created) == 1
    assert _remote_text(remote, result.staging_branch, "file1") == "master version"
    assert _remote_text(remote, result.staging_branch, "file2") == "master version"
    assert _remote_text(remote, result.staging_branch, "file3") == "master version"
    assert _remote_text(remote, result.release_branch, "file1") == "P2 version"
    assert _remote_branches(remote) == {
        "P2",
        "master",
        "reltest_30_08_2026",
        "release/30_08_2026_10_20_30_p2",
    }


def test_prepopulated_temporary_branch_still_creates_release_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path,
        "file1\nfile2\nfile3\n",
        prepopulate_temporary_branch=True,
    )
    before = _remote_sha(remote, "reltest_30_08_2026")

    result, backend = _run_promotion(runner)

    assert result.commit_sha == before
    assert _remote_sha(remote, result.staging_branch) == before
    assert result.pr is not None
    assert result.pr.head == "reltest_30_08_2026"
    assert result.pr.base == "release/30_08_2026_10_20_30_p2"
    assert len(backend.created) == 1
    assert result.release_branch in _remote_branches(remote)


def test_missing_promotion_txt_does_not_commit_push_or_create_a_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, None)
    before = _remote_sha(remote, "reltest_30_08_2026")
    backend = RecordingBackend()

    with pytest.raises(PromotionError) as caught:
        promote(
            repo_root=runner,
            deployment_target="P2",
            staging_branch="reltest_30_08_2026",
            git=Git(runner),
            pr_backend=backend,
        )

    assert caught.value.code == E_PROMOTION_FILE_MISSING
    assert "promotion.txt was not found in temporary branch 'reltest_30_08_2026'" in caught.value.message
    assert _remote_sha(remote, "reltest_30_08_2026") == before
    assert backend.created == []


def test_missing_source_file_fails_before_the_staging_branch_is_modified(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "file1\ndoes/not/exist.txt\n")
    before = _remote_sha(remote, "reltest_30_08_2026")
    backend = RecordingBackend()

    with pytest.raises(PromotionError) as caught:
        promote(
            repo_root=runner,
            deployment_target="P2",
            staging_branch="reltest_30_08_2026",
            git=Git(runner),
            pr_backend=backend,
        )

    assert caught.value.code == E_MISSING_SOURCE
    assert _remote_sha(remote, "reltest_30_08_2026") == before
    assert backend.created == []


def test_duplicate_promotion_txt_entry_fails_before_the_staging_branch_is_modified(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "file1\nfile1\n")
    before = _remote_sha(remote, "reltest_30_08_2026")

    with pytest.raises(PromotionError) as caught:
        _run_promotion(runner)

    assert caught.value.code == E_DUP_PATH
    assert _remote_sha(remote, "reltest_30_08_2026") == before


def test_workflow_promotion_rebuilds_workflows_list_on_the_staging_branch(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "workflows/new.json\n")

    result, _ = _run_promotion(runner)

    assert _remote_text(remote, result.staging_branch, "workflows/new.json") == '{"workflow": "new"}'
    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == "workflows/new.json"


def test_delete_from_promotion_txt_is_applied_to_the_existing_staging_branch(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "DELETE|workflows/old.json\n")

    result, _ = _run_promotion(runner)

    missing = subprocess.run(
        ["git", "show", f"{result.staging_branch}:workflows/old.json"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
