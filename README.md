# Sample repository

Sample content used to exercise the automated release promotion pipeline.

## Promotion flow

```
master  ->  psup  ->  prod
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

For a PSUP promotion, files named in `promotion.txt` are read from `master` and
applied to the supplied temporary branch. The pipeline validates the entire
inventory before it modifies the checkout, commits and pushes that temporary
branch, creates `release/<timestamp>_psup` from `psup`, then opens a Pull
Request from the temporary branch into the release branch. `master`, `psup` and
`prod` are never written to by the automation.

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
| `workflows_list.txt` | Rebuilt automatically whenever `workflows/` paths are promoted. |
| `promotion/` | The promotion pipeline itself. Standard library only. |
