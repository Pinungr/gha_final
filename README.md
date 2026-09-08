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

Application users run only `.github/workflows/code_promotion.yml`. The selected
`Use workflow from` ref is still passed as `${{ github.ref_name }}` and remains
the user-created staging branch. The parent workflow calls the existing
workflow files as local reusable workflows, so every lifecycle job appears
nested beneath the same Code Promotion run. No continuation is started with
`gh workflow run`, REST dispatch, or `workflow_run`. The org-owned DBX workflow
retains its manual `workflow_dispatch` interface and additionally exposes a
`workflow_call` interface for the parent.

The initial promotion PR carries a signed machine-readable promotion marker.
The parent validates at least one non-author approval, requests normal
non-admin squash auto-merge, and waits until GitHub reports the PR actually
merged. It never approves a PR or bypasses branch protection.

The parent then calls the lifecycle components in this order:

```text
prepare → initial PR approval/merge → merge verification → deployment
→ deployment verification → Environment validation (PSUP/PROD)
→ final PR creation/merge (PSUP/PROD) → summary
```

The reusable components are:

| Workflow | Purpose |
| --- | --- |
| `promotion_pr_approved.yml` | Polls the signed initial PR, validates the latest non-author approval, requests normal auto-merge, and waits for the actual merge. |
| `promotion_initial_merged.yml` | Verifies the merged PR identity and exact merge SHA before deployment. |
| `trigger_DBX_WF_management.yaml` | Personal-repository test stub: returns successful deployment outputs without changing DBX, ServiceNow, or organization resources. |
| `promotion_deployment_completed.yml` | Verifies the direct deployment result and exact branch/SHA, then reports whether validation is required. |
| `promotion_deployment_validation.yml` | Uses the configured GitHub Environment required-reviewer gate for PSUP/PROD. |
| `promotion_validation_completed.yml` | Creates and waits for the final synchronization PR after validation, or records rejection and prepares a parent-run rollback. |

MASTER skips validation and final synchronization. PSUP and PROD deploy their
timestamped release branch, require Environment approval, then create and merge
the final PR into the protected target branch. Release branches are retained.
For organization use, copy the corresponding file from
`office_workflow_templates/`. That version preserves the org DevSecOps,
ServiceNow, and DBX reusable jobs. When started individually, its existing
`workflow_dispatch` interface continues to offer `uat`, `psup`, and `prod` and
uses the selected Git ref. When called by Code Promotion, its separate
`workflow_call` interface receives `master`, `psup`, or `prod` and the exact
approved deployment branch as `repo_ref`.

Create the GitHub Environment `ReleaseApproval` and configure its required
reviewers (up to six users or teams, as needed). It is the shared post-deployment
approval gate for PSUP and PROD. Configure its wait timer and reviewer policy in
GitHub. The promotion remains paused until those Environment protection rules
pass; there is no scheduled controller or application-level expiry deadline.
GitHub Environment approval is platform-controlled and does not provide a
native 72-hour rejection deadline; configure the Environment wait timer and
reviewer policy to match the organization’s requirements.

The initial-PR and final-PR reusable jobs poll GitHub while the parent run is
active. This repository uses a five-hour polling deadline within the six-hour
GitHub-hosted job limit. On GitHub Enterprise Server or self-hosted runners,
verify the supported maximum job duration before increasing
`approval_timeout_hours` or the final-PR polling deadline.

Set repository secret `PROMOTION_LIFECYCLE_HMAC_KEY` to a random value of at
least 32 characters.
The initial workflow signs its metadata with this secret; continuation workflows
fail closed for unsigned or forged PR markers. Set repository secret
`REPO_TOKEN` to a fine-grained token for this repository with Contents,
Pull requests, Issues, and Workflows set to read and write (Metadata remains
read-only). A classic token needs `repo` and, when workflow files can be
promoted, `workflow`. The workflow checks both secrets and token access before
it can push a promotion branch. The token is used for protected merges and PR
comments. If PSUP/PROD branch rules prevent the final
synchronization PR from merging, grant only that automation identity a narrowly
scoped bypass for PRs carrying the signed final marker; do not grant that bypass
to the initial promotion PR.

Deployment uses environment-based concurrency (`dbx-deployment-${{ inputs.environment }}`),
so deployments to the same environment are serialized without creating separate
workflow runs.

The enterprise-ready workflow templates are kept in
`office_workflow_templates/`. They use the same reusable-call graph with
self-hosted runners and `github.kp.org` token handling. Unlike the active
personal-repository DBX stub, the office DBX template invokes the real shared
DevSecOps, ServiceNow, and DBX workflows.

The automation never supplies a branch-delete option. Ensure the repository's
automatic head-branch deletion setting is disabled (or exempts `release/*`), so
merged PSUP/PROD synchronization PRs cannot remove their audit release branch.
