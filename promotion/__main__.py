"""CLI entry point: ``python -m promotion``.

Inputs arrive as ``INPUT_*`` environment variables (how ``workflow_dispatch``
inputs are exposed) and can be overridden by flags for local runs. The selected
staging branch supplies the inventory through its root ``promotion.txt`` file.
Failures are rendered both as GitHub annotations and into the job summary.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from .errors import PromotionError
from .promote import PromotionResult, promote

EXIT_OK = 0
EXIT_PROMOTION_ERROR = 1
EXIT_UNEXPECTED = 2


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else None


def _run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def _append(var: str, text: str) -> None:
    path = os.environ.get(var)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text.rstrip("\n") + "\n")


def _set_outputs(result: PromotionResult) -> None:
    for key, value in (
        ("timestamp", result.timestamp),
        ("base_sha", result.base_sha),
        ("staging_branch", result.staging_branch),
        ("commit_sha", result.commit_sha or ""),
        ("pr_url", result.pr_url),
    ):
        _append("GITHUB_OUTPUT", f"{key}={value}")


def _summarise_success(result: PromotionResult) -> None:
    rows = [
        ("Environment", result.environment),
        ("Source branch", f"`{result.source_branch}`"),
        ("Target branch", f"`{result.target_branch}`"),
        ("Staging branch", f"`{result.staging_branch}`"),
        ("Baseline commit", f"`{result.base_sha}`"),
        ("Execution timestamp", f"`{result.timestamp}`"),
        ("Inventory", "`promotion.txt`"),
    ]
    lines = [
        "## Promotion prepared" if not result.dry_run else "## Dry run complete",
        "",
        "| Field | Value |",
        "| --- | --- |",
        *(f"| {label} | {value} |" for label, value in rows),
        "",
        f"### Changes ({len(result.changes)})",
        "",
        "| Status | Path |",
        "| --- | --- |",
        *(f"| `{status}` | `{path}` |" for status, path in result.changes),
        "",
    ]
    if result.pr_url:
        lines += [
            f"Pull Request: {result.pr_url}",
            "",
            "Squash and merge it once the configured approvals are satisfied.",
        ]
    elif result.dry_run:
        lines.append("No branches were pushed and no Pull Request was created.")
    _append("GITHUB_STEP_SUMMARY", "\n".join(lines))


def _summarise_failure(error: PromotionError) -> None:
    lines = [
        "## Promotion failed",
        "",
        f"**{error.code}** -- {error.message}",
        "",
    ]
    if error.details:
        lines += [*(f"- `{detail}`" for detail in error.details), ""]
    if error.remedy:
        lines += [f"**What to do:** {error.remedy}", ""]
    lines.append(
        "No Pull Request was created. The source-of-truth branches were not "
        "modified."
    )
    _append("GITHUB_STEP_SUMMARY", "\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m promotion",
        description="Promote the inventory in a staging branch and open a Pull Request.",
    )
    parser.add_argument("--target", help="Deployment target, e.g. PSUP or PROD.")
    parser.add_argument(
        "--staging-branch",
        help="Existing user-created branch containing root promotion.txt.",
    )
    parser.add_argument("--description", help="Optional Pull Request description.")
    parser.add_argument(
        "--repo-root",
        default=os.environ.get("GITHUB_WORKSPACE") or ".",
        help="Repository checkout to operate on (default: current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and stage everything, then stop before any push.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    target = args.target or _env("INPUT_DEPLOYMENT_TARGET")
    staging_branch = args.staging_branch or _env("INPUT_STAGING_BRANCH")
    description = args.description or _env("INPUT_RELEASE_DESCRIPTION")

    try:
        result = promote(
            repo_root=Path(args.repo_root),
            deployment_target=target,
            staging_branch=staging_branch,
            release_description=description,
            run_url=_run_url(),
            dry_run=args.dry_run,
            log=lambda message: print(message, flush=True),
        )
    except PromotionError as error:
        print(f"::error title={error.code}::{error.message}", flush=True)
        for detail in error.details:
            print(f"::error::{detail}", flush=True)
        if error.remedy:
            print(f"::notice::{error.remedy}", flush=True)
        print(f"\n{error}", file=sys.stderr, flush=True)
        _summarise_failure(error)
        return EXIT_PROMOTION_ERROR
    except Exception:  # noqa: BLE001 - last resort, must still report cleanly
        print(
            "::error title=E_INTERNAL::The promotion pipeline hit an unexpected "
            "error. No Pull Request was created.",
            flush=True,
        )
        traceback.print_exc()
        _append(
            "GITHUB_STEP_SUMMARY",
            "## Promotion failed\n\nAn unexpected internal error occurred. See "
            "the job log for the traceback. No protected branch was modified.",
        )
        return EXIT_UNEXPECTED

    _set_outputs(result)
    _summarise_success(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
