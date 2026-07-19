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
research-agent sources verify PROJECT
research-agent sources rebuild-index PROJECT
research-agent sources backup ./backups/2026-07-17
```

The Web material center uses the same `SourceService` as these commands and the Worker. Uploads are copied from seekable multipart streams into content-addressed storage without a second whole-file application-memory read. Agent tools require `project_id` and cannot read arbitrary paths.

For server deployments, set `SOURCE_API_KEYS_JSON` to a JSON key-to-project map. Every Source API request must then include `X-Source-API-Key`. Configure `SOURCE_EMBEDDING_BASE_URL`, `SOURCE_EMBEDDING_API_KEY`, and `SOURCE_EMBEDDING_MODEL` to enable real semantic vector scores; without them the system explicitly reports semantic score `0` and uses keyword, synonym, number normalization and structural ranking only.

## Server checklist

- Run the Worker as a separate process with a persistent SQLite/object volume, or replace the ports with a service-compatible repository/object store adapter.
- Put LibreOffice and Tesseract with `chi_sim` and `eng` trained data in the worker image.
- Restrict the data volume and configure `SOURCE_API_KEYS_JSON` or an upstream identity-aware project ACL.
- Monitor upload bytes, parser/OCR failures, queue retries, stale heartbeats, search latency, and Quality Gate statuses.
- Use `sources verify`, `sources backup` and `sources rebuild-index` during backups, migration and recovery. Backups refuse to overwrite an existing object directory.
