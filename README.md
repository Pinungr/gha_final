# Sample repository

Sample content used to exercise the automated release promotion pipeline.

## Promotion flow

```
master  ->  psup  ->  prod
```

Dispatch the `code_promotion` workflow from Actions, pick `PSUP` or `PROD`, and
select the user-created staging branch. Before dispatching, create that branch
from the selected target branch and commit a root-level `promotion.txt` file
with one repository-relative path per line. `DELETE|<path>` remains available
for deletions.

```
staging/customer_release_001/promotion.txt

config/application.yml
workflows/customer_sync.json
DELETE|workflows/legacy_cleanup.json
```

For a PSUP promotion, files named in `promotion.txt` are read from `master` and
applied to the same staging branch. The pipeline validates the entire inventory
before it modifies the checkout, commits and pushes that existing staging
branch, then opens a Pull Request from it into `psup`. It never creates
`temp/*` or `release/*` branches. `master`, `psup` and `prod` are never written to
by the automation.

The `promotion.txt` file must be at the repository root of the selected staging
branch. Blank lines and surrounding whitespace are ignored; invalid or duplicate
paths fail the run without a push or Pull Request.

## Layout

| Path | Contents |
| --- | --- |
| `config/` | Application, database and feature-flag configuration. |
| `Notebooks/` | Jupyter notebooks. |
| `workflows/` | Job definitions. Drives the `workflows_list.txt` rebuild. |
| `test/` | Tests for the sample content. |
| `workflows_list.txt` | Rebuilt automatically whenever `workflows/` paths are promoted. |
| `promotion/` | The promotion pipeline itself. Standard library only. |
