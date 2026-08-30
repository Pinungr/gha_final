"""Parsing and validation of the ``promotion.txt`` inventory (BRD section 11).

Everything here runs before the first git write. A malformed inventory must fail
the run while the repository is still untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .errors import (
    E_ABS_PATH,
    E_BAD_PATH,
    E_CONFLICT_PATH,
    E_DUP_PATH,
    E_NO_INPUT,
    E_TRAVERSAL,
    PromotionError,
)

DELETE_PREFIX = "DELETE|"

# Newline is the supported separator. ';' remains accepted for backwards
# compatibility with existing staged inventories created from the former GHA
# input; it never bypasses path validation.
SECONDARY_SEPARATOR = ";"

PROMOTE = "promote"
DELETE = "delete"

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Entry:
    """One requested change."""

    path: str
    action: str  # PROMOTE or DELETE
    line_no: int
    raw: str
    is_workflow: bool
    location: str = ""

    @property
    def is_delete(self) -> bool:
        return self.action == DELETE


@dataclass(frozen=True)
class Inventory:
    """The validated promotion set for a single run."""

    entries: tuple[Entry, ...]

    @property
    def promotes(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.action == PROMOTE)

    @property
    def deletes(self) -> tuple[Entry, ...]:
        return tuple(e for e in self.entries if e.action == DELETE)

    @property
    def promote_paths(self) -> list[str]:
        return [e.path for e in self.promotes]

    @property
    def delete_paths(self) -> list[str]:
        return [e.path for e in self.deletes]

    @property
    def all_paths(self) -> list[str]:
        return [e.path for e in self.entries]

    @property
    def workflow_promote_paths(self) -> list[str]:
        """Promoted workflow paths, in input order.

        DELETE entries are excluded: ``workflows_list.txt`` records what this
        release *ships*, and a deleted workflow is not shipped. See
        docs/BRD-traceability.md for why section 10 is read this way.
        """
        return [e.path for e in self.promotes if e.is_workflow]


def _classify_path(path: str) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when ``path`` is unusable, else ``None``."""
    if not path:
        return E_BAD_PATH, "path is empty"
    if _CONTROL_RE.search(path):
        return E_BAD_PATH, "path contains control characters"
    if path.startswith("/") or path.startswith("\\\\") or _DRIVE_RE.match(path):
        return E_ABS_PATH, "absolute paths are not accepted"
    if "\\" in path:
        return E_BAD_PATH, "use '/' as the path separator, not '\\'"
    segments = path.split("/")
    if ".." in segments:
        return E_TRAVERSAL, "path traversal ('..') is not accepted"
    if "." in segments:
        return E_BAD_PATH, "'.' path segments are not accepted"
    if "" in segments:
        return E_BAD_PATH, "path has an empty segment (leading, trailing or double '/')"
    if path != path.strip():
        return E_BAD_PATH, "path has leading or trailing whitespace"
    return None


def _split(text: str | None) -> list[tuple[int, str, str]]:
    """Yield ``(line_no, location, raw_entry)`` for every non-blank entry."""
    items: list[tuple[int, str, str]] = []
    for line_no, line in enumerate((text or "").splitlines(), start=1):
        parts = line.split(SECONDARY_SEPARATOR)
        for index, part in enumerate(parts, start=1):
            raw = part.strip()
            if not raw:  # section 11: blank lines may be ignored
                continue
            location = (
                f"line {line_no}"
                if len(parts) == 1
                else f"line {line_no} item {index}"
            )
            items.append((line_no, location, raw))
    return items


def parse(text: str | None, cfg: Config) -> Inventory:
    """Parse ``promotion.txt`` into a validated inventory."""
    parsed: list[Entry] = []
    malformed: dict[str, list[str]] = {}

    def reject(code: str, location: str, raw: str, reason: str) -> None:
        malformed.setdefault(code, []).append(f"{location}: {raw!r} -- {reason}")

    for line_no, location, raw in _split(text):
        if raw.startswith(DELETE_PREFIX):
            action, path = DELETE, raw[len(DELETE_PREFIX) :].strip()
        elif "|" in raw and raw.split("|", 1)[0].strip().upper() == "DELETE":
            reject(
                E_BAD_PATH,
                location,
                raw,
                f"deletion marker must be written exactly as "
                f"'{DELETE_PREFIX}<path>' (uppercase, no spaces)",
            )
            continue
        else:
            action, path = PROMOTE, raw

        problem = _classify_path(path)
        if problem is not None:
            reject(problem[0], location, raw, problem[1])
            continue

        parsed.append(
            Entry(
                path=path,
                action=action,
                line_no=line_no,
                raw=raw,
                is_workflow=cfg.is_workflow_path(path),
                location=location,
            )
        )

    if malformed:
        # One category at a time, but every offending line within it, so a single
        # re-run can fix all of them.
        for code in (E_ABS_PATH, E_TRAVERSAL, E_BAD_PATH):
            if code in malformed:
                raise PromotionError(
                    code,
                    f"{len(malformed[code])} invalid path(s) in 'promotion.txt'.",
                    details=malformed[code],
                    remedy="Use repository-relative paths with '/' separators, "
                    f"joined by '{SECONDARY_SEPARATOR}' or newlines.",
                )

    if not parsed:
        raise PromotionError(
            E_NO_INPUT,
            "'promotion.txt' contained no file paths.",
            remedy="Add at least one repository-relative path. Separate "
            f"multiple paths with '{SECONDARY_SEPARATOR}' or newlines.",
        )

    _check_duplicates(parsed)
    _check_list_file_conflict(parsed, cfg)
    return Inventory(entries=tuple(parsed))


def _check_list_file_conflict(entries: list[Entry], cfg: Config) -> None:
    """Reject requests that fight the automation for ``workflows_list.txt``.

    When the request promotes workflow paths, section 10 makes the pipeline the
    author of that file. A user entry for the same path would be silently
    overwritten -- or, for a ``DELETE``, silently turned into a modification.
    """
    if not any(e.action == PROMOTE and e.is_workflow for e in entries):
        return
    clashes = [e for e in entries if e.path == cfg.workflows_list_file]
    if not clashes:
        return
    raise PromotionError(
        E_CONFLICT_PATH,
        f"{cfg.workflows_list_file} cannot be listed in 'promotion.txt' when "
        f"the request also promotes workflow paths.",
        details=[
            f"{e.location}: {e.raw} ({'deletion' if e.is_delete else 'promotion'})"
            for e in clashes
        ],
        remedy=f"Remove that line. The pipeline rebuilds "
        f"{cfg.workflows_list_file} automatically from the workflow paths in "
        f"this request.",
    )


def _check_duplicates(entries: list[Entry]) -> None:
    """Reject repeated paths and promote/delete conflicts (sections 11 and 15)."""
    grouped: dict[str, list[Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.path, []).append(entry)

    conflicts: list[str] = []
    duplicates: list[str] = []
    for path, group in grouped.items():
        if len(group) == 1:
            continue
        at = ", ".join(e.location for e in group)
        if len({e.action for e in group}) > 1:
            conflicts.append(
                f"{path} -- declared both for promotion and for deletion ({at})"
            )
        else:
            duplicates.append(f"{path} -- listed {len(group)} times ({at})")

    if not conflicts and not duplicates:
        return

    if conflicts:
        code = E_CONFLICT_PATH
        message = (
            f"{len(conflicts)} path(s) requested for both promotion and deletion"
            + (f", and {len(duplicates)} duplicate path(s)" if duplicates else "")
            + " in 'promotion.txt'."
        )
    else:
        code = E_DUP_PATH
        message = f"{len(duplicates)} duplicate path(s) in 'promotion.txt'."

    raise PromotionError(
        code,
        message,
        details=conflicts + duplicates,
        remedy="List each path exactly once, either as a promotion or as a "
        f"'{DELETE_PREFIX}' deletion.",
    )
