"""PSUP/PROD-specific staging validation.

Every staging file must match the configured promotion source. Additional
changes are permitted only when they are byte-identical to that source; any
workflow change not listed in ``promotion.txt`` is still rejected by the shared
guard.
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
    """Validate the PSUP/PROD route's staging branch policy."""
    additional_changes = common_guards.additional_staging_changes(
        changes=changes,
        inventory=inventory,
        cfg=cfg,
        metadata_paths=metadata_paths,
    )
    common_guards.validate_additional_source_matches(
        git=git,
        additional_changes=additional_changes,
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
        preserve_staging_workflows=False,
    )
    return promotes, deletes, additional_changes
