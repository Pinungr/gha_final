"""MASTER-specific staging validation.

MASTER preserves workflow changes already prepared on staging, whether or not
they are listed in ``promotion.txt``. Declared non-workflow files and additional
non-workflow staging changes must match ``dev_collaboration`` before the PR can
be created.
"""

from __future__ import annotations

from .. import guards as common_guards
from ..config import Config
from ..inventory import Inventory


def validate_staging_changes(
    *,
    git: object,
    changes: list[tuple[str, str]],
    inventory: Inventory,
    cfg: Config,
    source_rev: str,
    staging_rev: str,
    metadata_paths: set[str],
) -> tuple[set[str], set[str], list[tuple[str, str]]]:
    """Validate the MASTER route's staging branch policy."""
    additional_changes = common_guards.additional_staging_changes(
        changes=changes,
        inventory=inventory,
        cfg=cfg,
        metadata_paths=metadata_paths,
        allow_unlisted_workflows=True,
    )
    common_guards.validate_additional_source_matches(
        git=git,
        additional_changes=[
            (status, path)
            for status, path in additional_changes
            if not cfg.is_workflow_path(path)
        ],
        source_rev=source_rev,
        staging_rev=staging_rev,
    )
    promotes, deletes = common_guards.validate_declared_staging_changes(
        git=git,
        changes=changes,
        inventory=inventory,
        cfg=cfg,
        source_rev=source_rev,
        staging_rev=staging_rev,
        metadata_paths=metadata_paths,
        preserve_staging_workflows=True,
    )
    return promotes, deletes, additional_changes
