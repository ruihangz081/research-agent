# Source Infrastructure Operations

## Local

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[web,search]'
.venv/bin/research-agent-source-worker --data-dir .data/sources
.venv/bin/research-agent-web
```

The source catalog is SQLite at `SOURCE_DATA_DIR/catalog.sqlite3`; immutable raw objects are under `SOURCE_DATA_DIR/objects/<sha-prefix>/<sha-tail>`. Set `SOURCE_DATA_DIR` to a persistent volume in server deployments. Do not put `.env`, uploaded materials, `.data`, OCR temporary files, or `projects/` into Git.

## CLI

```bash
research-agent sources upload PROJECT report.pdf financials.xlsx
research-agent sources process --once --data-dir .data/sources
research-agent sources list PROJECT --all-versions
research-agent sources search PROJECT 'revenue 2025'
research-agent sources read PROJECT SOURCE_ID --chunk-id CHUNK_ID
research-agent sources inspect PROJECT SOURCE_ID
```

The Web material center uses the same `SourceService` as these commands and the Worker. Upload endpoints accept multipart request bodies; agent tools require `project_id` and cannot read arbitrary paths.

## Server checklist

- Run the Worker as a separate process with a persistent SQLite/object volume, or replace the ports with a service-compatible repository/object store adapter.
- Put LibreOffice and Tesseract with `chi_sim` and `eng` trained data in the worker image.
- Restrict the data volume and configure tenant/project ACLs at the API boundary.
- Monitor upload bytes, parser/OCR failures, queue retries, stale heartbeats, search latency, and Quality Gate statuses.
- Use `verify_consistency()` and `export_project()` during backups and migration.
