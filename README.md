# Sample repository

Sample content used to exercise the automated release promotion pipeline.

## Promotion flow

```
qa  ->  psup  ->  prod
```

Dispatch the `code_promotion` workflow from Actions, pick `PSUP` or `PROD`, and
list the repository-relative paths to promote. Separate paths with `;` — the
GitHub input box is single-line and will not accept newlines.

```
config/application.yml;workflows/customer_sync.json;DELETE|workflows/legacy_cleanup.json
```

The pipeline assembles exactly that change set on a generated `temp/*` branch,
opens a Pull Request into a matching `release/*` branch, and stops. `qa`, `psup`
and `prod` are never written to by the automation.

## Layout

| Path | Contents |
| --- | --- |
| `config/` | Application, database and feature-flag configuration. |
| `Notebooks/` | Jupyter notebooks. |
| `workflows/` | Job definitions. Drives the `workflows_list.txt` rebuild. |
| `test/` | Tests for the sample content. |
| `workflows_list.txt` | Rebuilt automatically whenever `workflows/` paths are promoted. |
| `promotion/` | The promotion pipeline itself. Standard library only. |
