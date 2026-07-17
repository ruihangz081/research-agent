# Legacy Source Migration

The cutover preserves old project material and reports before enabling the source infrastructure.

1. Copy a legacy project's `03_raw_data/` into a migration workspace. `import_legacy_raw_directory()` validates the project boundary, skips symlinks, registers immutable bytes, and preserves SHA-256/version history.
2. Parse, index, and activate imported sources through `SourceService` and `SourceWorker`.
3. Extract legacy `[src: ...]` references with `find_legacy_citations()`.
4. Provide an operator-reviewed mapping of legacy ID to `(source_id, chunk_id, exact_excerpt)`. `migrate_legacy_report()` creates an `EvidenceRecord` only when the excerpt is present in the current chunk and version.
5. Keep unresolved references in the migration result and block automatic cutover until an operator resolves them. No legacy source number is silently reused across files.
6. Archive the original report unchanged and generate the new report from EvidenceRecords and deterministic citations.

The migration helpers never accept arbitrary source paths from an Agent tool. They accept an explicit legacy project root, enforce `03_raw_data` containment, and record the actor as `migration` in the audit log.
