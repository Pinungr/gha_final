"""Safety assertions that back the BRD's non-negotiable invariants.

Section 14: the automation must never push to master, psup or prod.
Sections 6 and 20: the change set must contain nothing the user did not request,
beyond the one permitted ``workflows_list.txt`` rebuild.
"""

from __future__ import annotations

from .config import Config
from .errors import E_NO_CHANGES, E_PROTECTED_BRANCH, E_UNEXPECTED_CHANGE, PromotionError


def assert_push_allowed(branch: str, cfg: Config) -> None:
    """Refuse to push to a source-of-truth branch."""
    if cfg.is_protected(branch):
        raise PromotionError(
            E_PROTECTED_BRANCH,
            f"Refusing to push to protected branch {branch!r}.",
            details=[
                "The pipeline only ever pushes the user-supplied temporary branch "
                "or a generated release branch.",
            ],
            remedy="Select a non-protected temporary branch and check "
            "'protected_branches' in promotion.config.json.",
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
    """Verify the staged change set is exactly within the permitted path set."""
    if not changes:
        if not require_changes:
            return
        raise PromotionError(
            E_NO_CHANGES,
            "The temporary branch produces no change against the release baseline.",
            details=[
                "Every requested file is already identical on the release baseline.",
            ],
            remedy="Confirm the temporary branch contains the intended promotion "
            "changes, then re-run.",
        )

    allowed = set(requested_paths)
    allowed.update(additional_allowed_paths or [])
    if allow_workflows_list:
        allowed.add(cfg.workflows_list_file)

    unexpected = sorted({path for _, path in changes if path not in allowed})
    if unexpected:
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            f"{len(unexpected)} file(s) changed that were not requested.",
            details=unexpected,
            remedy="No Pull Request was created and no protected branch was "
            "modified. Report this: the pipeline must only touch requested paths.",
        )
