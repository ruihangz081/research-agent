---
name: brokerage-report-formatting
description: Format verified Chinese research into a professional brokerage-style report with consistent structure, chart manifests, tables, citations, disclosures, and delivery checks. Use when Agent5 creates a final report, when an existing report needs brokerage-style formatting, or when chart, table, HTML, LaTeX, or PDF presentation needs repair.
---

# Brokerage Report Formatting

Turn verified analysis into a publication-ready Chinese brokerage-style report without changing its evidence or conclusions.

## Workflow

1. Read the verified Agent4 analysis.
2. Read the chart-specific references before writing the chart manifest:
   - `references/chart-rules.md`
   - `references/quality-checklist.md`
3. Preserve every deterministic evidence citation exactly. Do not invent facts, data, sources, ratings, or forecasts.
4. Treat Agent4 Markdown as immutable. Write only `05_chart_manifest.json`; the formatter copies the analysis verbatim and inserts chart placeholders deterministically.
5. Set `placement_after` to one exact, unique full line from Agent4 Markdown, preferably a heading. The formatter inserts `{{chart:<id>}}` after it. Never emit ASCII charts, executable code, raw SVG, TikZ, or “建议插入图表”.
6. Prefer a table when readers need exact values; use a chart only when it materially improves comparison or pattern recognition.
7. Give every chart a conclusion-led title, unit, as-of date, source, and any necessary note.
8. Run the delivery checklist before finishing. Omit a chart when Agent4 lacks sufficient verified data.

## Output Contract

Write a JSON manifest with this shape:

```json
{
  "version": 1,
  "charts": [
    {
      "id": "market_growth",
      "type": "line",
      "title": "市场规模保持稳健增长",
      "unit": "亿元",
      "as_of_date": "2026-07-19",
      "source": "公开资料，Research Agent 整理",
      "placement_after": "## 市场规模保持稳健增长",
      "labels": ["2023", "2024", "2025E"],
      "series": [
        {
          "name": "市场规模",
          "values": [100, 118, 136],
          "value_kind": ["actual", "actual", "forecast"]
        }
      ]
    }
  ]
}
```

Use only numeric values already present in verified input. Supported deterministic chart types are `line`, `bar`, `stacked_bar`, `combo`, `scatter`, `heatmap`, and `waterfall`. Use a descriptive unsupported type only when these cannot express a necessary chart; the renderer will request a constrained Vega-Lite fallback.

## Boundaries

- Do not conduct new research or revise Agent4 conclusions.
- Do not summarize, compress, reorder, or append evidence material to Agent4 Markdown.
- Do not convert deterministic source tokens into fabricated footnote numbers.
- Do not place URLs, file paths, code, or expressions in chart data.
- Do not use decorative charts, 3D effects, gradients, gauges, or pie charts with many categories.
- Do not hide a failed required chart; retain the underlying table or report the failure.
- Use the templates in `assets/` through the report renderer; do not reproduce or modify their LaTeX in the report body.
