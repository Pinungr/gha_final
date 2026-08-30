"""Loading and validation of ``promotion.config.json``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from .errors import (
    E_BAD_CONFIG,
    E_BAD_STAGING_BRANCH,
    E_BAD_TARGET,
    E_NO_TARGET,
    PromotionError,
)

CONFIG_FILENAME = "promotion.config.json"

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# A fixed offset rather than a named zone: zoneinfo needs system tzdata, which
# Windows does not ship, so a named zone would break local dry-runs and pull in
# a dependency this package deliberately avoids. Zones observing DST therefore
# need this value changed twice a year; India (+05:30) does not observe DST.
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to an anchored regex.

    ``*`` and ``?`` stay inside one path segment; ``**/`` spans zero or more
    leading segments and a trailing ``**`` spans the rest of the path. This is
    written out by hand rather than delegated to :mod:`fnmatch`, whose ``*``
    crosses ``/`` and would classify ``src/workflow/a.json`` as a workflow path.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**", i):
            i += 2
            if i < n and pattern[i] == "/":
                out.append("(?:[^/]+/)*")
                i += 1
            else:
                out.append(".*")
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class Environment:
    """One promotion route and its Pull Request base strategy."""

    name: str
    source: str
    target: str
    create_release_branch: bool

    @property
    def slug(self) -> str:
        """Lowercase suffix used in generated release branch names."""
        return self.name.lower()


@dataclass(frozen=True)
class Config:
    environments: dict[str, Environment]
    protected_branches: tuple[str, ...]
    workflow_path_pattern: str
    workflows_list_file: str
    timestamp_offset: timedelta
    _workflow_re: re.Pattern[str]

    @property
    def timestamp_tz(self) -> timezone:
        """Timezone timestamps in PR and audit records are stamped in."""
        return timezone(self.timestamp_offset)

    def resolve(self, target_name: str | None) -> Environment:
        """Map a ``deployment_target`` input to its promotion route."""
        if not target_name or not target_name.strip():
            raise PromotionError(
                E_NO_TARGET,
                "No deployment target was supplied.",
                remedy=f"Re-run the workflow and select one of: "
                f"{', '.join(sorted(self.environments))}.",
            )
        key = target_name.strip().upper()
        if key not in self.environments:
            raise PromotionError(
                E_BAD_TARGET,
                f"Unknown deployment target {target_name.strip()!r}.",
                remedy=f"Select one of: {', '.join(sorted(self.environments))}.",
            )
        return self.environments[key]

    def is_workflow_path(self, path: str) -> bool:
        """True when ``path`` belongs to the workflow path pattern (section 10)."""
        return self._workflow_re.match(path) is not None

    def is_protected(self, branch: str) -> bool:
        return branch in self.protected_branches


def _require_str(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PromotionError(
            E_BAD_CONFIG,
            f"{CONFIG_FILENAME}: {field!r} must be a non-empty string.",
        )
    return raw.strip()


def _require_branch(raw: object, field: str) -> str:
    value = _require_str(raw, field)
    if not _BRANCH_RE.match(value) or ".." in value:
        raise PromotionError(
            E_BAD_CONFIG,
            f"{CONFIG_FILENAME}: {field!r} is not a valid branch name: {value!r}.",
        )
    return value


def validate_branch_name(value: str, field: str = "branch") -> str:
    """Validate a user-supplied branch name before it reaches git."""
    try:
        return _require_branch(value, field)
    except PromotionError as error:
        if error.code != E_BAD_CONFIG:  # pragma: no cover - defensive only
            raise
        raise PromotionError(
            E_BAD_STAGING_BRANCH,
            f"{field} is not a valid branch name: {value!r}.",
            remedy="Select an existing repository branch, for example "
            "'staging/customer_release_001'.",
        ) from None


def _require_offset(raw: object, field: str) -> timedelta:
    """Parse a ``+HH:MM`` / ``-HH:MM`` UTC offset. Absent means UTC."""
    if raw is None:
        return timedelta(0)
    value = _require_str(raw, field)
    match = _OFFSET_RE.match(value)
    if not match:
        raise PromotionError(
            E_BAD_CONFIG,
            f"{CONFIG_FILENAME}: {field!r} must look like '+05:30' or '-08:00', "
            f"not {value!r}.",
        )
    sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes))
    if offset > timedelta(hours=14):
        raise PromotionError(
            E_BAD_CONFIG,
            f"{CONFIG_FILENAME}: {field!r} is outside the valid UTC offset "
            f"range (-12:00 to +14:00): {value!r}.",
        )
    return -offset if sign == "-" else offset


def load(repo_root: Path, filename: str = CONFIG_FILENAME) -> Config:
    path = repo_root / filename
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PromotionError(
            E_BAD_CONFIG,
            f"{filename} was not found at the repository root ({path}).",
            remedy=f"Add {filename} to the repository. See docs/INSTALL.md.",
        ) from None
    except json.JSONDecodeError as exc:
        raise PromotionError(
            E_BAD_CONFIG, f"{filename} is not valid JSON: {exc}."
        ) from None

    if not isinstance(raw, dict):
        raise PromotionError(E_BAD_CONFIG, f"{filename} must contain a JSON object.")

    raw_envs = raw.get("environments")
    if not isinstance(raw_envs, dict) or not raw_envs:
        raise PromotionError(
            E_BAD_CONFIG,
            f"{filename}: 'environments' must be a non-empty object.",
        )

    environments: dict[str, Environment] = {}
    for name, spec in raw_envs.items():
        label = _require_str(name, "environments key").upper()
        if not isinstance(spec, dict):
            raise PromotionError(
                E_BAD_CONFIG,
                f"{filename}: environments.{name} must be an object with "
                f"'source' and 'target'.",
            )
        source = _require_branch(spec.get("source"), f"environments.{name}.source")
        target = _require_branch(spec.get("target"), f"environments.{name}.target")
        if source == target:
            raise PromotionError(
                E_BAD_CONFIG,
                f"{filename}: environments.{name} has the same source and "
                f"target branch ({source!r}).",
            )
        create_release_branch = spec.get("create_release_branch", True)
        if not isinstance(create_release_branch, bool):
            raise PromotionError(
                E_BAD_CONFIG,
                f"{filename}: environments.{name}.create_release_branch must be "
                "a boolean when supplied.",
            )
        environments[label] = Environment(
            name=label,
            source=source,
            target=target,
            create_release_branch=create_release_branch,
        )

    raw_protected = raw.get("protected_branches", [])
    if not isinstance(raw_protected, list):
        raise PromotionError(
            E_BAD_CONFIG, f"{filename}: 'protected_branches' must be an array."
        )
    protected = tuple(
        _require_branch(b, "protected_branches[]") for b in raw_protected
    )

    pattern = _require_str(raw.get("workflow_path_pattern"), "workflow_path_pattern")
    list_file = _require_str(raw.get("workflows_list_file"), "workflows_list_file")
    if list_file.startswith("/") or "\\" in list_file or ".." in list_file.split("/"):
        raise PromotionError(
            E_BAD_CONFIG,
            f"{filename}: 'workflows_list_file' must be a repository-relative "
            f"path: {list_file!r}.",
        )

    return Config(
        environments=environments,
        protected_branches=protected,
        workflow_path_pattern=pattern,
        workflows_list_file=list_file,
        timestamp_offset=_require_offset(
            raw.get("timestamp_utc_offset"), "timestamp_utc_offset"
        ),
        _workflow_re=_glob_to_regex(pattern),
    )
