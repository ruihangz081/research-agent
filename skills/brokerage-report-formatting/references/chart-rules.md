# Chart Rules

Select charts by analytical intent:

| Intent | Type |
|---|---|
| Trend over time | `line` |
| Category comparison | `bar` |
| Composition over time | `stacked_bar` |
| Scale and rate together | `combo` |
| Relationship between two measures | `scatter` |
| Sensitivity or matrix comparison | `heatmap` |
| Incremental contribution | `waterfall` |

Rules:

- Use one chart for one principal conclusion.
- Prefer 1-3 charts per quantitative chapter and no more than the configured report maximum.
- Use conclusion-led titles, not neutral labels such as “市场规模图”.
- Provide `unit`, ISO `as_of_date`, and a human-readable `source` for every chart.
- Keep label and series lengths equal. Use JSON numbers or `null` only.
- Mark each point `actual`, `forecast`, or `estimate`; forecast points must be visually distinguishable.
- Avoid dual axes unless `combo` is essential and units are explicit.
- Do not infer missing points, interpolate, combine incompatible units, or copy a number from an unverified narrative.
- Use a table instead of a chart when exact values, long labels, or more than two units are central.
- Use an unsupported chart type only for a necessary layered, faceted, repeated, or concatenated view. Never request executable drawing code.
