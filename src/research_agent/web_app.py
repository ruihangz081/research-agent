"""Local web UI for running research projects."""
from __future__ import annotations

import asyncio
import re
from html import escape
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .agent_loop import AgentOptions, run_agent
from .agents import analyst, collector, formatter, strategist, validator
from .llm import LLMClient
from .orchestrator import _safe_run
from .report_layout import (
    FILE_FINAL_REPORT_TEX,
    FILE_FINAL_REPORT_TYPESET_PDF,
    generate_typeset_artifacts,
)
from .state import ProjectState, Stage
from .tools import default_registry
from .sources.api import build_runtime, create_sources_router

STATIC_DIR = Path(__file__).parent / "web_static"
ARTIFACT_LIMIT = 120_000
FINAL_REPORT_PDF = "05_final_report.pdf"

app = FastAPI(title="Research Agent Web")
_source_service, _source_queue = build_runtime(config.SOURCE_DATA_DIR)
app.include_router(create_sources_router(_source_service, _source_queue))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/materials", include_in_schema=False)
async def materials_center() -> FileResponse:
    return FileResponse(STATIC_DIR / "materials.html")

JOBS: dict[str, dict[str, Any]] = {}
LOCKS: dict[str, asyncio.Lock] = {}


class CreateProjectRequest(BaseModel):
    topic: str
    brief: str = ""
    max_collect_rounds: int = Field(default=3, ge=1, le=5)


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = ""


def _project_id(project_dir: Path) -> str:
    return project_dir.name


def _project_dir(project_id: str) -> Path:
    path = (config.PROJECTS_DIR / project_id).resolve()
    root = config.PROJECTS_DIR.resolve()
    if root != path and root not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid project id")
    return path


def _load_state(project_id: str) -> ProjectState:
    try:
        return ProjectState.load(_project_dir(project_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _job(project_id: str) -> dict[str, Any]:
    return JOBS.setdefault(
        project_id,
        {
            "running": False,
            "status": "idle",
            "message": "",
            "logs": [],
            "updated_at": datetime.now().isoformat(),
        },
    )


def _log(project_id: str, message: str) -> None:
    job = _job(project_id)
    job["message"] = message
    job["updated_at"] = datetime.now().isoformat()
    job["logs"].append(
        {"time": datetime.now().strftime("%H:%M:%S"), "message": message}
    )
    job["logs"] = job["logs"][-300:]


def _artifact_paths(state: ProjectState) -> list[tuple[str, str, Path | None]]:
    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    artifacts: list[tuple[str, str, Path | None]] = [
        ("outline", "调研提纲", Path(state.outline_path) if state.outline_path else None),
        (
            "sources_draft",
            "信息源草案",
            Path(state.sources_draft_path) if state.sources_draft_path else None,
        ),
        (
            "sources_final",
            "最终源清单",
            Path(state.sources_final_path) if state.sources_final_path else None,
        ),
        (
            "validation_report",
            "验证报告",
            Path(state.validation_report_path) if state.validation_report_path else None,
        ),
        ("analysis", "深度分析", Path(state.analysis_path) if state.analysis_path else None),
        (
            "final_report",
            "最终报告",
            Path(state.final_report_path) if state.final_report_path else None,
        ),
        (
            "final_report_tex",
            "LaTeX 源文件",
            Path(state.final_report_tex_path)
            if state.final_report_tex_path
            else state.project_dir / FILE_FINAL_REPORT_TEX,
        ),
        (
            "final_report_typeset_pdf",
            "高级排版 PDF",
            Path(state.final_report_typeset_pdf_path)
            if state.final_report_typeset_pdf_path
            else state.project_dir / FILE_FINAL_REPORT_TYPESET_PDF,
        ),
    ]
    if raw_dir.exists():
        for path in sorted(raw_dir.glob("round_*.md")):
            artifacts.append((path.stem, f"采集数据 {path.stem}", path))
        for path in sorted(raw_dir.glob("feedback_round_*.json")):
            artifacts.append((path.stem, f"验证反馈 {path.stem}", path))
    return artifacts


def _read_artifact(path: Path | None) -> str:
    if not path or not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > ARTIFACT_LIMIT:
        return text[:ARTIFACT_LIMIT] + "\n\n[... truncated]"
    return text


def _plain_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return text.strip()


def _pdf_paragraph(text: str, style: Any) -> Any:
    from reportlab.platypus import Paragraph

    return Paragraph(escape(_plain_markdown(text)), style)


def _write_pdf_from_markdown(markdown_path: Path, pdf_path: Path, title: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import PageBreak, Preformatted, SimpleDocTemplate, Spacer

    base_font = "ResearchCJK"
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]
    for font_path in font_candidates:
        try:
            if Path(font_path).exists():
                pdfmetrics.registerFont(TTFont(base_font, font_path))
                break
        except Exception:
            continue
    else:
        base_font = "STSong-Light"
        pdfmetrics.registerFont(UnicodeCIDFont(base_font))

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CJKBody",
        parent=styles["BodyText"],
        fontName=base_font,
        fontSize=10.5,
        leading=17,
        wordWrap="CJK",
        spaceAfter=5,
    )
    h1 = ParagraphStyle(
        "CJKHeading1",
        parent=body,
        fontSize=18,
        leading=25,
        textColor=colors.HexColor("#17201b"),
        spaceBefore=8,
        spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "CJKHeading2",
        parent=body,
        fontSize=14,
        leading=21,
        textColor=colors.HexColor("#0f766e"),
        spaceBefore=10,
        spaceAfter=6,
    )
    h3 = ParagraphStyle(
        "CJKHeading3",
        parent=body,
        fontSize=12,
        leading=18,
        textColor=colors.HexColor("#255f99"),
        spaceBefore=8,
        spaceAfter=5,
    )
    code_style = ParagraphStyle(
        "CJKCode",
        parent=body,
        fontName=base_font,
        fontSize=9,
        leading=13,
        leftIndent=8,
        rightIndent=8,
        backColor=colors.HexColor("#f2f6f4"),
        borderColor=colors.HexColor("#dce3de"),
        borderWidth=0.5,
        borderPadding=6,
    )

    story: list[Any] = [_pdf_paragraph(title, h1), Spacer(1, 4 * mm)]
    in_code = False
    code_lines: list[str] = []
    text = markdown_path.read_text(encoding="utf-8")

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                story.append(Preformatted("\n".join(code_lines), code_style))
                story.append(Spacer(1, 3 * mm))
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw_line)
            continue
        if not stripped:
            story.append(Spacer(1, 2 * mm))
            continue
        if stripped == "---":
            story.append(Spacer(1, 5 * mm))
            continue
        if stripped.startswith("# "):
            story.append(_pdf_paragraph(stripped[2:], h1))
        elif stripped.startswith("## "):
            story.append(_pdf_paragraph(stripped[3:], h2))
        elif stripped.startswith("### "):
            story.append(_pdf_paragraph(stripped[4:], h3))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            story.append(_pdf_paragraph("• " + stripped[2:], body))
        elif re.match(r"^\d+\.\s+", stripped):
            story.append(_pdf_paragraph(stripped, body))
        elif stripped.startswith("|"):
            story.append(_pdf_paragraph(stripped, code_style))
        elif stripped == "\\pagebreak":
            story.append(PageBreak())
        else:
            story.append(_pdf_paragraph(stripped, body))

    if code_lines:
        story.append(Preformatted("\n".join(code_lines), code_style))

    def _footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(base_font, 8)
        canvas.setFillColor(colors.HexColor("#68756f"))
        canvas.drawRightString(200 * mm, 12 * mm, f"Page {doc.page}")
        canvas.restoreState()

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)


def _final_report_pdf_path(state: ProjectState) -> Path:
    final_report = Path(state.final_report_path) if state.final_report_path else None
    if not final_report or not final_report.exists():
        raise HTTPException(status_code=404, detail="最终报告还没有生成")

    pdf_path = state.project_dir / FINAL_REPORT_PDF
    if not pdf_path.exists() or final_report.stat().st_mtime > pdf_path.stat().st_mtime:
        try:
            _write_pdf_from_markdown(final_report, pdf_path, f"{state.topic} 调研报告")
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="缺少 reportlab，请重新安装：pip install -e '.[search,web]'",
            ) from exc
    return pdf_path


def _checkpoint_file(state: ProjectState) -> dict[str, str] | None:
    if state.stage == Stage.AWAIT_OUTLINE_APPROVAL:
        return {"key": "outline", "title": "调研提纲"}
    if state.stage == Stage.AWAIT_SOURCE_APPROVAL:
        return {"key": "sources_draft", "title": "信息源草案"}
    if state.stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL:
        return {"key": "sources_final", "title": "最终源清单"}
    return None


def _serialize_state(state: ProjectState) -> dict[str, Any]:
    project_id = _project_id(state.project_dir)
    job = _job(project_id)
    artifacts = []
    for key, label, path in _artifact_paths(state):
        artifacts.append(
            {
                "key": key,
                "label": label,
                "exists": bool(path and path.exists()),
                "name": path.name if path else "",
            }
        )

    return {
        "id": project_id,
        "topic": state.topic,
        "stage": state.stage.value,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "project_dir": str(state.project_dir),
        "running": job["running"],
        "job_status": job["status"],
        "job_message": job["message"],
        "logs": job["logs"],
        "checkpoint": _checkpoint_file(state),
        "artifacts": artifacts,
        "collect_round": state.collect_round,
        "max_collect_rounds": state.max_collect_rounds,
        "converged": state.converged,
    }


async def _run_web_strategist(state: ProjectState) -> Path:
    outline_path = state.project_dir / config.FILE_OUTLINE
    state.project_dir.mkdir(parents=True, exist_ok=True)

    system_prompt = strategist._load_system_prompt()
    system_prompt += (
        "\n\n## Web 模式要求\n"
        "- 不要向用户追问；根据主题和补充说明直接生成可审阅提纲。\n"
        "- 如果信息不足，使用合理默认值，并在提纲中标注“默认，可调整”。\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲写入路径（必须严格使用）：`{outline_path}`\n"
    )
    feedback = state.notes.pop("outline_feedback", None)
    if feedback:
        system_prompt += f"\n## 用户对上一版提纲的修改意见\n{feedback}\n"

    brief = state.notes.get("web_brief", "").strip()
    prompt = (
        f"请为「{state.topic}」生成行业调研提纲，并写入 `{outline_path}`。"
        "\n\n提纲需要包含调研目标、范围、核心问题、信息源策略、分析框架和最终交付结构。"
    )
    if brief:
        prompt += f"\n\n用户补充说明：\n{brief}"
    if feedback:
        prompt += f"\n\n请重点处理这条修改意见：{feedback}"

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=["Read", "Write"],
        cwd=str(state.project_dir),
        max_turns=20,
        stream=False,
    )
    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(prompt, options, client, default_registry)

    if not outline_path.exists():
        raise RuntimeError("Agent1 未能生成调研提纲。")
    return outline_path


async def _run_until_pause(project_id: str) -> None:
    lock = LOCKS.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        _log(project_id, "已有任务正在运行")
        return

    async with lock:
        job = _job(project_id)
        job["running"] = True
        job["status"] = "running"
        try:
            state = _load_state(project_id)
            await _run_state_machine(project_id, state)
            job["status"] = "idle"
        except Exception as exc:
            job["status"] = "error"
            _log(project_id, f"运行失败：{exc}")
        finally:
            job["running"] = False
            job["updated_at"] = datetime.now().isoformat()


async def _run_state_machine(project_id: str, state: ProjectState) -> None:
    while True:
        if state.stage == Stage.INIT:
            _log(project_id, "进入战略规划阶段")
            state.advance_to(Stage.PLANNING)

        if state.stage == Stage.PLANNING:
            _log(project_id, "Agent1 正在生成调研提纲")
            outline_path = await _safe_run(
                "Agent1·战略规划", state, _run_web_strategist, state
            )
            state.outline_path = str(outline_path)
            state.advance_to(Stage.AWAIT_OUTLINE_APPROVAL)
            _log(project_id, "调研提纲已生成，等待审批")
            return

        if state.stage == Stage.AWAIT_OUTLINE_APPROVAL:
            _log(project_id, "等待调研提纲审批")
            return

        if state.stage == Stage.SOURCING:
            _log(project_id, "Agent2 正在生成信息源草案")
            feedback = state.notes.pop("sources_feedback", None)
            path = await _safe_run(
                "Agent2·信息源分层",
                state,
                collector.run_source_tiering,
                state,
                feedback=feedback,
            )
            state.sources_draft_path = str(path)
            state.advance_to(Stage.AWAIT_SOURCE_APPROVAL)
            _log(project_id, "信息源草案已生成，等待审批")
            return

        if state.stage == Stage.AWAIT_SOURCE_APPROVAL:
            _log(project_id, "等待信息源草案审批")
            return

        if state.stage == Stage.COLLECTING_AND_VALIDATING:
            max_rounds = state.max_collect_rounds or config.MAX_COLLECT_ROUNDS
            _log(project_id, f"进入采集验证循环：{state.collect_round}/{max_rounds}")
            while state.collect_round < max_rounds and not state.converged:
                round_idx = state.collect_round + 1
                feedback_path = (
                    Path(state.last_feedback_path) if state.last_feedback_path else None
                )
                _log(project_id, f"Agent2 正在执行第 {round_idx} 轮采集")
                raw_path = await _safe_run(
                    f"Agent2·采集第{round_idx}轮",
                    state,
                    collector.run_collection_round,
                    state,
                    round_idx,
                    feedback_path=feedback_path,
                )
                _log(project_id, f"Agent3 正在验证第 {round_idx} 轮数据")
                fb_path, fb_obj = await _safe_run(
                    f"Agent3·验证第{round_idx}轮",
                    state,
                    validator.run_validation,
                    state,
                    round_idx,
                    raw_path,
                )
                state.collect_round = round_idx
                state.last_feedback_path = str(fb_path)
                state.converged = fb_obj.converged
                state.save()
                _log(
                    project_id,
                    f"第 {round_idx} 轮完成，收敛={fb_obj.converged}",
                )

            final_fb = validator.load_feedback(Path(state.last_feedback_path))
            path = await validator.finalize_sources(state, final_fb)
            state.sources_final_path = str(path)
            state.validation_report_path = str(
                state.project_dir / config.FILE_VALIDATION
            )
            state.advance_to(Stage.AWAIT_FINAL_SOURCE_APPROVAL)
            _log(project_id, "最终源清单已生成，等待审批")
            return

        if state.stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL:
            _log(project_id, "等待最终源清单审批")
            return

        if state.stage == Stage.ANALYZING:
            _log(project_id, "Agent4 正在生成深度分析")
            path = await _safe_run("Agent4·深度分析", state, analyst.run_analysis, state)
            state.analysis_path = str(path)
            state.advance_to(Stage.FORMATTING)

        if state.stage == Stage.FORMATTING:
            _log(project_id, "Agent5 正在排版最终报告")
            path = await _safe_run(
                "Agent5·排版交付", state, formatter.run_formatting, state
            )
            state.final_report_path = str(path)
            state.advance_to(Stage.DONE)

        if state.stage == Stage.DONE:
            _log(project_id, "调研完成")
            return


def _schedule(project_id: str) -> None:
    asyncio.create_task(_run_until_pause(project_id))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "model": config.LLM_MODEL,
        "base_url": config.LLM_BASE_URL,
        "has_api_key": bool(config.LLM_API_KEY),
        "projects_dir": str(config.PROJECTS_DIR),
    }


@app.get("/api/projects")
async def api_projects() -> dict[str, Any]:
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    projects = []
    for path in sorted(config.PROJECTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_dir() or not (path / config.FILE_STATE).exists():
            continue
        try:
            projects.append(_serialize_state(ProjectState.load(path)))
        except Exception:
            continue
    return {"projects": projects}


@app.post("/api/projects")
async def api_create_project(req: CreateProjectRequest) -> dict[str, Any]:
    topic = req.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Topic is required")

    state = ProjectState(topic=topic, date_str=datetime.now().strftime("%Y%m%d_%H%M%S"))
    state.max_collect_rounds = req.max_collect_rounds
    if req.brief.strip():
        state.notes["web_brief"] = req.brief.strip()
    state.save()

    project_id = _project_id(state.project_dir)
    _log(project_id, "项目已创建")
    _schedule(project_id)
    return _serialize_state(state)


@app.get("/api/projects/{project_id}")
async def api_project(project_id: str) -> dict[str, Any]:
    return _serialize_state(_load_state(project_id))


@app.post("/api/projects/{project_id}/continue")
async def api_continue(project_id: str) -> dict[str, Any]:
    _load_state(project_id)
    _schedule(project_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/approval")
async def api_approval(project_id: str, req: ApprovalRequest) -> dict[str, Any]:
    state = _load_state(project_id)
    feedback = req.feedback.strip() or "请重新优化"

    if state.stage == Stage.AWAIT_OUTLINE_APPROVAL:
        if req.approved:
            state.advance_to(Stage.SOURCING)
            _log(project_id, "调研提纲已通过")
        else:
            state.notes["outline_feedback"] = feedback
            state.advance_to(Stage.PLANNING)
            _log(project_id, "调研提纲已驳回，准备重跑")
    elif state.stage == Stage.AWAIT_SOURCE_APPROVAL:
        if req.approved:
            state.advance_to(Stage.COLLECTING_AND_VALIDATING)
            _log(project_id, "信息源草案已通过")
        else:
            state.notes["sources_feedback"] = feedback
            state.advance_to(Stage.SOURCING)
            _log(project_id, "信息源草案已驳回，准备重跑")
    elif state.stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL:
        if req.approved:
            state.advance_to(Stage.ANALYZING)
            _log(project_id, "最终源清单已通过")
        else:
            state.notes["sources_feedback"] = feedback
            state.collect_round = 0
            state.converged = False
            state.last_feedback_path = None
            state.advance_to(Stage.COLLECTING_AND_VALIDATING)
            _log(project_id, "最终源清单已驳回，准备重跑采集验证")
    else:
        raise HTTPException(status_code=400, detail="Project is not waiting for approval")

    _schedule(project_id)
    return _serialize_state(state)


@app.get("/api/projects/{project_id}/artifacts/{artifact_key}")
async def api_artifact(project_id: str, artifact_key: str) -> dict[str, Any]:
    state = _load_state(project_id)
    for key, label, path in _artifact_paths(state):
        if key == artifact_key:
            return {
                "key": key,
                "label": label,
                "exists": bool(path and path.exists()),
                "content": _read_artifact(path),
                "name": path.name if path else "",
            }
    raise HTTPException(status_code=404, detail="Artifact not found")


@app.get("/api/projects/{project_id}/download/final-report.pdf")
async def api_download_final_report_pdf(project_id: str) -> FileResponse:
    state = _load_state(project_id)
    pdf_path = _final_report_pdf_path(state)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{project_id}_final_report.pdf",
    )


@app.post("/api/projects/{project_id}/typeset/final-report")
async def api_typeset_final_report(project_id: str) -> dict[str, Any]:
    state = _load_state(project_id)
    final_report = Path(state.final_report_path) if state.final_report_path else None
    if not final_report or not final_report.exists():
        raise HTTPException(status_code=404, detail="最终报告还没有生成")

    try:
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report,
            force=True,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    state.final_report_tex_path = str(artifacts["tex_path"])
    if artifacts["pdf_path"]:
        state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
    state.save()

    return {
        "status": "pdf" if artifacts["pdf_path"] else "tex_only",
        "message": "已生成高级排版 PDF" if artifacts["pdf_path"] else "已生成 LaTeX 源文件；本机缺少 xelatex/lualatex，暂未编译 PDF",
        "has_engine": bool(artifacts["engine"]),
    }


@app.get("/api/projects/{project_id}/download/final-report.tex")
async def api_download_final_report_tex(project_id: str) -> FileResponse:
    state = _load_state(project_id)
    tex_path = (
        Path(state.final_report_tex_path)
        if state.final_report_tex_path
        else state.project_dir / FILE_FINAL_REPORT_TEX
    )
    if not tex_path.exists():
        final_report = Path(state.final_report_path) if state.final_report_path else None
        if not final_report or not final_report.exists():
            raise HTTPException(status_code=404, detail="最终报告还没有生成")
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report,
        )
        tex_path = artifacts["tex_path"]
        state.final_report_tex_path = str(tex_path)
        if artifacts["pdf_path"]:
            state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
        state.save()

    return FileResponse(
        tex_path,
        media_type="application/x-tex",
        filename=f"{project_id}_final_report.tex",
    )


@app.get("/api/projects/{project_id}/download/final-report-typeset.pdf")
async def api_download_typeset_pdf(project_id: str) -> FileResponse:
    state = _load_state(project_id)
    pdf_path = (
        Path(state.final_report_typeset_pdf_path)
        if state.final_report_typeset_pdf_path
        else state.project_dir / FILE_FINAL_REPORT_TYPESET_PDF
    )
    if not pdf_path.exists():
        final_report = Path(state.final_report_path) if state.final_report_path else None
        if not final_report or not final_report.exists():
            raise HTTPException(status_code=404, detail="最终报告还没有生成")
        artifacts = await generate_typeset_artifacts(
            topic=state.topic,
            project_dir=state.project_dir,
            final_report_path=final_report,
        )
        if not artifacts["pdf_path"]:
            raise HTTPException(
                status_code=409,
                detail="已生成 LaTeX 源文件，但本机缺少 xelatex/lualatex，无法编译高级 PDF",
            )
        pdf_path = artifacts["pdf_path"]
        state.final_report_tex_path = str(artifacts["tex_path"])
        state.final_report_typeset_pdf_path = str(pdf_path)
        state.save()

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{project_id}_final_report_typeset.pdf",
    )


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="research-agent-web",
        description="启动 Research Agent 本地网页工作台",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    uvicorn.run(
        "research_agent.web_app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
