---
name: brokerage-report-formatting
description: Format verified Chinese research into a professional brokerage-style report with consistent structure, chart manifests, tables, citations, disclosures, and delivery checks. Use when Agent5 creates a final report, when an existing report needs brokerage-style formatting, or when chart, table, HTML, LaTeX, or PDF presentation needs repair.
---

# Brokerage Report Formatting

Turn verified analysis into a publication-ready Chinese brokerage-style report without changing its evidence or conclusions.

## Workflow

1. Read the outline, verified analysis, source list, and validation report.
2. Read all references in this skill before writing the final report:
   - `references/report-structure.md`
   - `references/chart-rules.md`
   - `references/table-rules.md`
   - `references/china-style.md`
   - `references/quality-checklist.md`
3. Preserve every deterministic evidence citation exactly. Do not invent facts, data, sources, ratings, or forecasts.
4. Write the complete final Markdown report and a separate `05_chart_manifest.json`.
5. Use `{{chart:<id>}}` on its own line where each chart belongs. Never emit ASCII charts, executable code, raw SVG, TikZ, or “建议插入图表”.
6. Prefer a table when readers need exact values; use a chart only when it materially improves comparison or pattern recognition.
7. Give every chart and table a conclusion-led title, unit, as-of date, source, and any necessary note.
8. Run the delivery checklist before finishing. Leave an explicit data-gap statement when evidence is insufficient.

## Output Contract

Write valid Markdown suitable for Pandoc. Keep headings at three levels or fewer, use GFM pipe tables, and keep chart placeholders outside tables and lists.

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
- Do not convert deterministic source tokens into fabricated footnote numbers.
- Do not place URLs, file paths, code, or expressions in chart data.
- Do not use decorative charts, 3D effects, gradients, gauges, or pie charts with many categories.
- Do not hide a failed required chart; retain the underlying table or report the failure.
- Use the templates in `assets/` through the report renderer; do not reproduce or modify their LaTeX in the report body.
