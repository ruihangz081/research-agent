"""LaTeX report layout helpers for Agent5 and the web UI."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import config
from .llm import LLMClient
from .llm.types import ChatMessage

FILE_FINAL_REPORT_TEX = "05_final_report.tex"
FILE_FINAL_REPORT_TYPESET_PDF = "05_final_report_typeset.pdf"


def strip_latex_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:latex|tex)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def find_latex_engine() -> str | None:
    for name in ("xelatex", "lualatex"):
        found = shutil.which(name)
        if found:
            return found
        texbin = Path("/Library/TeX/texbin") / name
        if texbin.exists():
            return str(texbin)
    return None


def compile_latex(tex_path: Path) -> Path | None:
    engine = find_latex_engine()
    if not engine:
        return None

    for _ in range(2):
        proc = subprocess.run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=tex_path.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            log_path = tex_path.with_suffix(".compile.log")
            log_path.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
            raise RuntimeError(f"LaTeX 编译失败，日志：{log_path.name}")

    pdf_path = tex_path.with_suffix(".pdf")
    log_path = tex_path.with_suffix(".compile.log")
    if log_path.exists():
        log_path.unlink()
    return pdf_path if pdf_path.exists() else None


async def generate_latex_from_markdown(
    *,
    topic: str,
    markdown_path: Path,
    tex_path: Path,
    client: LLMClient | None = None,
) -> Path:
    source = markdown_path.read_text(encoding="utf-8")
    if len(source) > 80_000:
        source = source[:80_000] + "\n\n[内容过长，已截断用于排版生成]"

    system_prompt = (
        "你是资深中文投研报告排版设计师和 LaTeX 工程师。"
        "请把用户提供的 Markdown 投研报告重排为完整可编译的 XeLaTeX 文档。"
        "必须只输出 LaTeX 源码，不要输出解释。"
    )
    user_prompt = f"""
请将下面的 Markdown 报告转换为专业中文投研报告 LaTeX 文档。

硬性要求：
- 使用 \\documentclass[UTF8,fontset=fandol,11pt]{{ctexart}}，不要依赖系统中文字体。
- 使用 geometry、xcolor、booktabs、longtable、tabularx、array、hyperref、fancyhdr、titlesec、enumitem、tikz、pgfplots。
- 可根据报告中的关键数字主动绘制 2-4 个图：例如股价区间、收入结构、情景预测、风险矩阵。优先用 tikz 或 pgfplots，不要引用外部图片。
- 表格要用 booktabs/longtable/tabularx 重新排版，不要保留 Markdown 竖线表格。
- 文字密集矩阵、SWOT、风险清单、机会/风险对照表、超过 4 行或每格超过 20 个中文字符的内容，必须使用 tabularx 或 longtable，不要用 TikZ 固定坐标画成图。
- 只把数字型图表、时间线、流程图、少量节点关系图交给 tikz/pgfplots；TikZ 节点必须设置 text width 和足够间距，避免重叠。
- 首页要有标题、主题、生成日期、摘要信息块。
- 正文要有清晰章节层级、页眉页脚、页码、目录。
- 所有 \\begin{{...}} 和 \\end{{...}} 必须严格配对，尤其 titlepage、center、minipage、tikzpicture、axis、longtable、tabularx。
- 不要在 center 环境内再嵌套 center；需要居中时优先使用 \\centering。
- 不要使用 minted、shell-escape、外部网络资源或本地图片。
- 避免过度复杂的宏，确保 XeLaTeX 尽量容易编译。
- 如果引用来源编号如 [15]，原样保留。

报告标题：{topic} 调研报告

Markdown 原文：
{source}
"""

    async def _call(active_client: LLMClient) -> str:
        resp = await active_client.chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_prompt),
            ],
            temperature=0.2,
        )
        return resp.content or ""

    if client:
        latex = await _call(client)
    else:
        async with LLMClient(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            model=config.LLM_MODEL,
            timeout=config.LLM_TIMEOUT,
            max_retries=config.LLM_MAX_RETRIES,
        ) as active_client:
            latex = await _call(active_client)

    latex = strip_latex_fence(latex)
    if "\\documentclass" not in latex:
        raise RuntimeError("模型没有生成有效 LaTeX 文档")

    tex_path.write_text(latex, encoding="utf-8")
    return tex_path


async def generate_typeset_artifacts(
    *,
    topic: str,
    project_dir: Path,
    final_report_path: Path,
    client: LLMClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    tex_path = project_dir / FILE_FINAL_REPORT_TEX
    pdf_path = project_dir / FILE_FINAL_REPORT_TYPESET_PDF

    if force or not tex_path.exists() or final_report_path.stat().st_mtime > tex_path.stat().st_mtime:
        await generate_latex_from_markdown(
            topic=topic,
            markdown_path=final_report_path,
            tex_path=tex_path,
            client=client,
        )

    compiled = compile_latex(tex_path)
    if compiled and compiled.exists() and compiled != pdf_path:
        shutil.copyfile(compiled, pdf_path)
        compiled = pdf_path
    return {
        "tex_path": tex_path,
        "pdf_path": compiled if compiled and compiled.exists() else None,
        "engine": find_latex_engine(),
    }
