# Sample repository

Sample content used to exercise the automated release promotion pipeline.

## Promotion flow

```
dev_collaboration  ->  master  ->  psup  ->  prod
```

Dispatch the `code_promotion` workflow from Actions, pick `PSUP` or `PROD`, and
select the user-created temporary branch. Before dispatching, create that
branch and commit a root-level `promotion.txt` file with one repository-relative
path per line. `DELETE|<path>` remains available for deletions.

```
reltest_30_08_2026/promotion.txt

config/application.yml
workflows/customer_sync.json
DELETE|workflows/legacy_cleanup.json
```

For a MASTER promotion, files named in `promotion.txt` are read from
`dev_collaboration`, applied to the supplied temporary branch, and proposed by
a Pull Request from that branch directly to `master`. No release branch is
created for MASTER.

PSUP and PROD keep the release-branch path: their requested files are read from
`master` and `psup` respectively, applied to the temporary branch, then the
pipeline creates `release/<timestamp>_psup` or `release/<timestamp>_prod` from
the target and opens a Pull Request into that generated release branch.
`dev_collaboration`, `master`, `psup`, and `prod` are never written to by the
automation.

The `promotion.txt` file must be at the repository root of the selected staging
branch. Blank lines and surrounding whitespace are ignored; invalid or duplicate
paths fail the run without a push or Pull Request. If the temporary branch
already has the requested `master` versions, the automation does not create another
commit; it still creates the release branch and Pull Request after verifying the
full PR diff contains only approved paths.

## Layout

| Path | Contents |
| --- | --- |
| `config/` | Application, database and feature-flag configuration. |
| `Notebooks/` | Jupyter notebooks. |
| `workflows/` | Job definitions. Drives the `workflows_list.txt` rebuild. |
| `test/` | Tests for the sample content. |
| `workflows_list.txt` | Rebuilt fresh from only workflow files that actually change in the PR. |
| `promotion/` | The promotion pipeline itself. Standard library only. |

Neither target-branch nor staging-branch list entries are carried into the new
list. For MASTER, workflow changes are taken from the staging branch and every
workflow actually present in the resulting PR is recorded. Non-workflow files
continue to be checked against `dev_collaboration`. PSUP and PROD retain their
stricter source-match validation.

## Promotion flow modules

The shared engine in `promotion/promote.py` dispatches route-specific rules to:

| Module | Responsibility |
| --- | --- |
| `promotion/master/guards.py` | MASTER staging validation: workflow changes are read from staging; non-workflow changes must match `dev_collaboration`. |
| `promotion/master/promote.py` | Direct staging-to-master Pull Request; no release branch. |
| `promotion/psup_prod/guards.py` | PSUP/PROD validation: every staging file must match the configured source branch. |
| `promotion/psup_prod/promote.py` | PSUP/PROD timestamped release-branch planning. |

Git operations, inventory parsing, Pull Request generation, branch safety, and
`workflows_list.txt` generation remain shared so the two flows cannot drift.

## Approval, deployment, and validation lifecycle

The initial promotion PR carries a signed machine-readable promotion marker.
After at least one non-author approval, GitHub's own branch protection and
required-check rules remain authoritative: the automation requests a normal
non-admin squash auto-merge and waits for the PR's merged event. It never
approves a PR or bypasses the initial review gate.

After that merge, the following workflows continue the lifecycle without a
runner waiting for people:

| Workflow | Purpose |
| --- | --- |
| `promotion_pr_approved.yml` | Validates an approval for a signed promotion PR and requests its protected merge. |
| `promotion_initial_merged.yml` | Dispatches `trigger_DBX_WF_management.yaml` for `master` or the generated release branch. |
| `trigger_DBX_WF_management.yaml` | Provides the DBX deployment-action structure. Until Databricks commands are supplied, every action is an explicitly logged successful no-op. |
| `promotion_deployment_completed.yml` | Starts post-deployment validation only after a successful DBX workflow run. |
| `promotion_deployment_validation.yml` | Uses the configured GitHub Environment required-reviewer gate. MASTER completes after approval; PSUP/PROD create a signed final synchronization PR. |
| `promotion_validation_timeout.yml` | Runs hourly, cancels validations not approved within 24 hours, and records `VALIDATION_EXPIRED`. |
| `promotion_validation_completed.yml` | Records an Environment rejection/cancellation and prevents finalization. |

Create the GitHub Environment `ReleaseApproval` and configure its required
reviewers (up to six users or teams, as needed). It is the shared post-deployment
approval gate for MASTER, PSUP, and PROD; its name and the 24-hour deadline are
configured in `promotion.config.json` under `lifecycle`.

Set repository secret `PROMOTION_LIFECYCLE_HMAC_KEY` to a long random value.
The initial workflow signs its metadata with this secret; continuation workflows
fail closed for unsigned or forged PR markers. The GitHub Actions token needs
the workflow permissions declared in each lifecycle YAML. Also set
`PROMOTION_AUTOMATION_TOKEN` to a GitHub App installation token or approved
service token with the declared repository permissions. It is used for merges,
PR comments, and workflow dispatch so the next event-driven workflow is not
suppressed as a same-token event. If PSUP/PROD branch rules prevent the final
synchronization PR from merging, grant only that automation identity a narrowly
scoped bypass for PRs carrying the signed final marker; do not grant that bypass
to the initial promotion PR.

The automation never supplies a branch-delete option. Ensure the repository's
automatic head-branch deletion setting is disabled (or exempts `release/*`), so
merged PSUP/PROD synchronization PRs cannot remove their audit release branch.
