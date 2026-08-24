# Chart Rules

Select charts by analytical intent:

| Intent | Type |
|---|---|
| Trend over time | `line` |
| Category comparison | `bar` |
| Long-name ranking | `horizontal_bar` |
| Scenario / forecast lower-upper bounds | `range_bar` |
| Composition over time | `stacked_bar` |
| Scale and rate together | `combo` |
| Relationship between two measures | `scatter` |
| Sensitivity or matrix comparison | `heatmap` |
| Incremental contribution | `waterfall` |

Rules:

- Use one chart for one principal conclusion.
- Set `placement_after` to one exact, unique full line from Agent4 Markdown; prefer the relevant heading. **Two or more charts must never share the same `placement_after`** — the program hard-blocks duplicate anchors (v2), and historical manifests already contain collisions, so check each anchor against the others before writing.
- Prefer 1-3 charts per quantitative chapter and no more than the configured report maximum.
- Use conclusion-led titles, not neutral labels such as “市场规模图”.
- Provide `unit`, ISO `as_of_date`, and a human-readable `source` for every chart.
- Keep label and series lengths equal. Use JSON numbers or `null` only.
- Mark each point `actual`, `forecast`, or `estimate`; forecast points must be visually distinguishable.
- Avoid dual axes unless `combo` is essential and units are explicit.
- Do not infer missing points, interpolate, combine incompatible units, or copy a number from an unverified narrative.
- Use a table instead of a chart when exact values, long labels, or more than two units are central.
- Use an unsupported chart type only for a necessary layered, faceted, repeated, or concatenated view. Never request executable drawing code.

## Visual declarations (declarative only)

The manifest is declarative — never emit drawing code. Use these fields to express intent:

- `visual.orientation`: `auto` (default) / `vertical` / `horizontal`. `auto` switches to horizontal automatically for long labels or many categories.
- `visual.show_values`: boolean, show key value labels.
- `visual.highlight_labels` / `visual.highlight_series`: names to emphasize (must match existing labels/series).
- `visual.number_format`: `auto` / `integer` / `decimal_1` / `percent_1` / `multiple_1`.
- `visual.legend_position`: `auto` / `top` / `right` / `none`.

- `reference_lines`: semantic reference lines. `axis` is `value` (fixed number + optional text label) or `category` (bind an existing label). Never use physical canvas coordinates.
- `bands`: value interval bands with `lower` / `upper` (lower < upper), for forecast/reasonable-value/threshold ranges.
- `callouts`: point annotations bound to an existing `label` plus `text`.

## Provenance

`provenance.claim_ids` is a candidate clue list of `claim_id`s from `04_claims.json`. The program reverse-checks each claim (exists, still in Agent4 text, backed by SUPPORTED evidence) and matches every chart value against claim/evidence text. Values that cannot be matched block delivery. Do not self-declare `evidence_ids` or per-point mappings.

List the claims that actually carry the chart's numbers — normally `kind` of `fact` or `derivation`. A zero-evidence `judgment` claim carries no numbers, so listing it adds nothing; the program drops such candidates and records the reason.
