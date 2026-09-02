"""Safety assertions for promotion validation."""

from __future__ import annotations

from .config import Config
from .errors import (
    E_NO_CHANGES,
    E_PROTECTED_BRANCH,
    E_STAGING_SOURCE_MISMATCH,
    E_UNEXPECTED_CHANGE,
    PromotionError,
)
from .inventory import DELETE, Inventory


def assert_push_allowed(branch: str, cfg: Config) -> None:
    """Refuse to push to a protected source-of-truth branch."""
    if cfg.is_protected(branch):
        raise PromotionError(
            E_PROTECTED_BRANCH,
            f"Refusing to push to protected branch {branch!r}.",
            details=[
                "The pipeline only pushes a user-supplied staging branch or a "
                "generated release branch.",
            ],
            remedy=(
                "Select a non-protected staging branch and check "
                "'protected_branches' in promotion.config.json."
            ),
        )


def assert_changes_expected(
    changes: list[tuple[str, str]],
    requested_paths: list[str],
    cfg: Config,
    allow_workflows_list: bool,
    *,
    additional_allowed_paths: list[str] | None = None,
    require_changes: bool = True,
) -> None:
    """Verify the final change set contains only permitted paths."""
    if not changes:
        if not require_changes:
            return

        raise PromotionError(
            E_NO_CHANGES,
            "The temporary branch produces no change against the release baseline.",
            details=[
                "Every requested file is already identical on the release baseline.",
            ],
            remedy=(
                "Confirm the temporary branch contains the intended changes, "
                "then re-run."
            ),
        )

    allowed = set(requested_paths)
    allowed.update(additional_allowed_paths or [])

    if allow_workflows_list:
        allowed.add(cfg.workflows_list_file)

    unexpected = sorted(
        {
            path
            for _, path in changes
            if path not in allowed
        }
    )

    if unexpected:
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            f"{len(unexpected)} file(s) changed that were not requested.",
            details=unexpected,
            remedy=(
                "No Pull Request was created and no protected branch was modified."
            ),
        )


def additional_staging_changes(
    *,
    changes: list[tuple[str, str]],
    inventory: Inventory,
    cfg: Config,
    metadata_paths: set[str],
    allow_unlisted_workflows: bool,
) -> list[tuple[str, str]]:
    """Return additional staging changes permitted by the selected route."""

    entry_by_path = {
        entry.path: entry
        for entry in inventory.entries
    }

    additional_changes = sorted(
        (status, path)
        for status, path in changes
        if path not in metadata_paths
        and path not in entry_by_path
    )

    unrequested_workflow_changes = [
        (status, path)
        for status, path in additional_changes
        if cfg.is_workflow_path(path)
    ]
    if unrequested_workflow_changes and not allow_unlisted_workflows:
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            (
                f"{len(unrequested_workflow_changes)} workflow file(s) differ from "
                "the target baseline but are not listed in promotion.txt."
            ),
            details=[
                f"{status} {path}"
                for status, path in unrequested_workflow_changes
            ],
            remedy=(
                "Create the staging branch from the current target branch and "
                "list every workflow intended for this Pull Request in "
                "promotion.txt. No workflow was deleted or pushed."
            ),
        )

    staging_list_changes = [
        (status, path)
        for status, path in changes
        if path == cfg.workflows_list_file
    ]
    if staging_list_changes and not any(
        entry.is_workflow for entry in inventory.entries
    ):
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            (
                f"{cfg.workflows_list_file} differs from the target baseline, but "
                "the current promotion.txt contains no workflow request."
            ),
            details=[f"{status} {path}" for status, path in staging_list_changes],
            remedy=(
                "Create the staging branch from the current target branch. The "
                "workflow list is maintained automatically only when this run "
                "promotes or deletes a workflow. Nothing was pushed."
            ),
        )

    return additional_changes


def validate_declared_staging_changes(
    *,
    git: object,
    changes: list[tuple[str, str]],
    inventory: Inventory,
    cfg: Config,
    source_rev: str,
    staging_rev: str,
    metadata_paths: set[str],
    preserve_staging_workflows: bool,
) -> tuple[set[str], set[str]]:
    """Validate declared files and return those already prepared in staging.

    Each route chooses whether a workflow already prepared on its staging
    branch is preserved or must match its configured source branch.
    """
    entry_by_path = {entry.path: entry for entry in inventory.entries}
    manually_prepared_promotes: set[str] = set()
    manually_prepared_deletes: set[str] = set()
    mismatches: list[str] = []

    for status, path in changes:
        if path in metadata_paths:
            continue
        if path not in entry_by_path:
            continue

        entry = entry_by_path[path]
        if entry.action == DELETE:
            if status[:1] != "D":
                mismatches.append(
                    f"{path}: declared DELETE but is not deleted "
                    "in the temporary branch"
                )
            else:
                manually_prepared_deletes.add(path)

            continue

        if status[:1] == "D":
            mismatches.append(
                f"{path}: declared for promotion but is deleted "
                "in the temporary branch"
            )
            continue

        if preserve_staging_workflows and cfg.is_workflow_path(path):
            manually_prepared_promotes.add(path)
            continue

        source_kind = git.object_type(
            source_rev,
            path,
        )

        staging_kind = git.object_type(
            staging_rev,
            path,
        )

        if source_kind != "blob" or staging_kind != "blob":
            mismatches.append(
                f"{path}: does not exist as a file in both the temporary and "
                "approved source branches"
            )
            continue

        if (
            git.read_file_bytes(staging_rev, path)
            != git.read_file_bytes(source_rev, path)
        ):
            mismatches.append(
                f"{path}: temporary-branch content does not match approved source "
                f"'{source_rev.rsplit('/', 1)[-1]}'"
            )
            continue

        manually_prepared_promotes.add(path)

    _raise_source_mismatch(mismatches)
    return manually_prepared_promotes, manually_prepared_deletes


def validate_additional_source_matches(
    *,
    git: object,
    additional_changes: list[tuple[str, str]],
    source_rev: str,
    staging_rev: str,
) -> None:
    """Require preserved additional files to be byte-identical to the source."""
    mismatches: list[str] = []
    for status, path in additional_changes:
        if status[:1] == "D":
            mismatches.append(
                f"{path}: staging deletion must be declared in promotion.txt"
            )
            continue

        source_kind = git.object_type(source_rev, path)
        staging_kind = git.object_type(staging_rev, path)
        if source_kind != "blob" or staging_kind != "blob":
            mismatches.append(
                f"{path}: staging content does not exist as a file in both "
                "the temporary and approved source branches"
            )
            continue

        if git.read_file_bytes(staging_rev, path) != git.read_file_bytes(source_rev, path):
            mismatches.append(
                f"{path}: staging content does not match approved source "
                f"'{source_rev.rsplit('/', 1)[-1]}'"
            )

    _raise_source_mismatch(mismatches)


def _raise_source_mismatch(mismatches: list[str]) -> None:
    if not mismatches:
        return

    raise PromotionError(
        E_STAGING_SOURCE_MISMATCH,
        (
            "Validation failed: manually prepared temporary-branch files "
            "do not match the approved promotion source."
        ),
        details=mismatches,
        remedy=(
            "Reset the listed files to the approved source content, or remove "
            "them from promotion.txt and the temporary branch."
        ),
    )
