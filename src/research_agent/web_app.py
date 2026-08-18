"""Local web UI for running research projects."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, run_log, token_usage
from .agent_loop import AgentOptions, run_agent
from .agents import strategist
from .llm import ChatMessage, LLMClient
from .orchestrator import (
    CheckpointDecision,
    CheckpointResult,
    CheckpointSpec,
    DeliveryBlockedError,
    PipelineError,
    ResearchPlanBlockedError,
    RERUN_STAGE_LABELS,
    StrategistOutcome,
    _append_clarification,
    _assert_delivery_ready,
    available_rerun_stages,
    checkpoint_for,
    migrate_research_plan,
    prepare_stage_rerun,
    prepare_retry,
    recover_blocked_delivery,
    research_plan_migration_required,
    retry_blocked_reason,
    run_state_machine,
)
from .report_layout import (
    FILE_FINAL_REPORT_TEX,
    generate_typeset_artifacts,
)
from .research_plan import ResearchPlanError, load_plan_or_none
from .state import ProjectState, Stage
from .tools import default_registry
from .tools.builtins.web_search import SUPPORTED_PROVIDERS, web_search
from .sources.api import build_runtime, create_sources_router
from .sources.tasks import TasksError, config_tasks_path, load_tasks_file

STATIC_DIR = Path(__file__).parent / "web_static"
ARTIFACT_LIMIT = 120_000
HTML_HEADERS = {"Cache-Control": "no-store"}
ENV_PATH = config.PROJECT_ROOT / ".env"

# 静态资源版本号：所有 HTML 里的 ?v= 应引用同一个常量，保证 CSS/JS 只缓存一份。
# 修改任意 web_static 资源后 bump 此值即可让浏览器刷新缓存。
STATIC_VERSION = "20260818-perf10"

# 静态资源长缓存：资源 URL 携带 STATIC_VERSION，改内容即改版本号，
# 因此可安全缓存较长时间，避免每次 304 revalidate。
STATIC_CACHE_HEADERS = {"Cache-Control": f"public, max-age=31536000, immutable"}


class _CachedStaticFiles(StaticFiles):
    """给 /static 资源附加长缓存头（HTML 保持 no-store，由版本号控制失效）。"""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.update(STATIC_CACHE_HEADERS)
        return response


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    recovered = _recover_interrupted_projects()
    if recovered:
        print(
            f"[lumitrace] 已标记 {len(recovered)} 个被中断的项目为可重试："
            f"{', '.join(recovered)}"
        )
    yield


app = FastAPI(title="Lumitrace Web", lifespan=_lifespan)

AUTH_COOKIE = "lumitrace_token"
AUTH_HEADER = "X-Auth-Token"


def _is_loopback(host: str) -> bool:
    """判断绑定地址是否只对本机可见。"""
    value = (host or "").strip().lower()
    if value in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def _require_token(request: Request, call_next):
    """配置了 WEB_AUTH_TOKEN 时校验每个请求。

    令牌可通过 `X-Auth-Token` 头、`?token=` 查询参数或 cookie 提供。用查询参数
    首次访问会写入 cookie，之后浏览器无需再带参数。未配置令牌时不做校验，
    此时服务应只绑定回环地址（由 `main()` 强制）。
    """
    token = config.WEB_AUTH_TOKEN
    if not token:
        return await call_next(request)

    supplied = (
        request.headers.get(AUTH_HEADER)
        or request.query_params.get("token")
        or request.cookies.get(AUTH_COOKIE)
    )
    if not supplied or not secrets.compare_digest(supplied, token):
        return JSONResponse(
            status_code=401,
            content={"detail": "需要有效的访问令牌（X-Auth-Token 头或 ?token= 参数）"},
        )

    response = await call_next(request)
    if request.query_params.get("token") == token:
        response.set_cookie(
            AUTH_COOKIE, token, httponly=True, samesite="strict", path="/"
        )
    return response
_source_service, _source_queue = build_runtime(config.SOURCE_DATA_DIR)
app.include_router(create_sources_router(_source_service, _source_queue, process_in_background=True))
if STATIC_DIR.exists():
    app.mount("/static", _CachedStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/materials", include_in_schema=False)
async def materials_center() -> FileResponse:
    return FileResponse(STATIC_DIR / "materials.html", headers=HTML_HEADERS)


@app.get("/workspace", include_in_schema=False)
async def project_workspace() -> FileResponse:
    return FileResponse(STATIC_DIR / "workspace.html", headers=HTML_HEADERS)


@app.get("/research", include_in_schema=False)
async def research_home() -> FileResponse:
    return FileResponse(STATIC_DIR / "research.html", headers=HTML_HEADERS)


@app.get("/results", include_in_schema=False)
async def results_center() -> FileResponse:
    return FileResponse(STATIC_DIR / "results.html", headers=HTML_HEADERS)


@app.get("/settings", include_in_schema=False)
async def settings_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "settings.html", headers=HTML_HEADERS)


# ── SPA 视图片段 ────────────────────────────────────────────────
# SPA 壳（app.html）通过 /api/views/{name} 拉取各页面的主内容区 HTML 片段，
# 复用现有 5 个 HTML 文件的单一事实来源，避免内容双重维护。
_VIEW_FILES: dict[str, str] = {
    "research": "research.html",
    "workspace": "workspace.html",
    "materials": "materials.html",
    "results": "results.html",
    "settings": "settings.html",
}
_VIEW_TITLES: dict[str, str] = {
    "research": "研究首页",
    "workspace": "项目工作区",
    "materials": "材料中心",
    "results": "成果中心",
    "settings": "设置",
}


def _view_fragment(name: str) -> dict[str, str]:
    """从既有 HTML 文件切出主内容区片段（含 <main> 内的顶栏与内容区，不含脚本）。

    切法：定位 `<main class="main-column">` 到配对的 `</main>`，去掉其中的
    <script> 块（脚本由 SPA 壳统一加载）。找不到对应文件或标记时抛 404。
    """
    if name not in _VIEW_FILES:
        raise HTTPException(status_code=404, detail=f"Unknown view: {name}")
    path = STATIC_DIR / _VIEW_FILES[name]
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"View not found: {name}")
    text = path.read_text(encoding="utf-8")

    start = text.find('<main class="main-column">')
    if start == -1:
        raise HTTPException(status_code=404, detail=f"View has no main: {name}")
    end = text.find("</main>", start)
    if end == -1:
        raise HTTPException(status_code=404, detail=f"View main not closed: {name}")

    # 片段 = <main> 标签内部的内容（不含 <main>/</main> 标签本身：SPA 壳已有
    # 一个 <main class="main-column"> 作为 #viewMount 的父容器，重复包含会形成
    # 嵌套 main 并干扰视图过渡），再加上 </main> 之后、第一个 <script> 之前的
    # 兄弟元素（抽屉/弹层等，视图 JS 的 bindEvents 依赖它们）。
    inner_start = start + len('<main class="main-column">')
    script_start = text.find("<script", end)
    if script_start == -1:
        script_start = len(text)
    fragment = text[inner_start:end] + text[end + len("</main>"):script_start]

    # 去掉 <script> 块：脚本统一由 SPA 壳加载，片段内不应再执行一次
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", "", fragment, flags=re.DOTALL)

    return {
        "name": name,
        "title": _VIEW_TITLES.get(name, name),
        "html": fragment,
    }


@app.get("/api/views/{name}", include_in_schema=False)
async def api_view_fragment(name: str, response: Response) -> dict[str, str]:
    response.headers.update(HTML_HEADERS)
    return _view_fragment(name)


@app.get("/app", include_in_schema=False)
async def spa_shell() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.html", headers=HTML_HEADERS)


@app.get("/app/{path:path}", include_in_schema=False)
async def spa_shell_path(path: str) -> FileResponse:
    """SPA 前端路由：/app/ 下的任意路径都回退到同一壳，由前端 router 解析。"""
    return FileResponse(STATIC_DIR / "app.html", headers=HTML_HEADERS)

JOBS: dict[str, dict[str, Any]] = {}
LOCKS: dict[str, asyncio.Lock] = {}
CONFIG_LOCK = asyncio.Lock()
# 持有后台任务的强引用：事件循环只持弱引用，不保管会导致任务可能被 GC 回收
BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
# SSE 订阅者：project_id -> 每个连接一个 asyncio.Queue，项目状态变化时广播
SUBSCRIBERS: dict[str, set[asyncio.Queue[str]]] = {}


class CreateProjectRequest(BaseModel):
    topic: str
    brief: str = ""
    max_collect_rounds: int | None = Field(default=None, ge=1, le=5)
    output_preference: Literal["fast", "balanced", "deep"] | None = None


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = ""


class RetryRequest(BaseModel):
    extra_rounds: int = Field(default=1, ge=0, le=5)


class RerunRequest(BaseModel):
    stage: Literal[
        "planning",
        "sourcing",
        "collecting_and_validating",
        "analyzing",
        "formatting",
    ]


class ClarificationRequest(BaseModel):
    answers: list[str] = Field(default_factory=list)
    skip: bool = False


class ModelConfigRequest(BaseModel):
    base_url: str = Field(min_length=1)
    api_key: str | None = None
    model: str = Field(min_length=1)
    timeout: float = Field(default=120, ge=1, le=600)
    max_retries: int = Field(default=3, ge=1, le=10)
    temperature: float = Field(default=0.7, ge=0, le=2)


class WorkspaceConfigRequest(BaseModel):
    default_rounds: int = Field(ge=1, le=5)
    output_preference: Literal["fast", "balanced", "deep"]
    projects_dir: str = Field(min_length=1)
    source_data_dir: str = Field(min_length=1)


class SearchConfigRequest(BaseModel):
    provider: Literal["anysearch", "duckduckgo", "serpapi", "tavily"]
    api_key: str | None = None


def _validate_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL must start with http:// or https://")
    return base_url


def _format_env_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:+\-=]*", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _write_env_updates(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        key = match.group(1) if match else None
        if key in remaining:
            output.append(f"{key}={_format_env_value(remaining.pop(key))}")
        else:
            output.append(line)
    if remaining and output and output[-1].strip():
        output.append("")
    output.extend(f"{key}={_format_env_value(value)}" for key, value in remaining.items())
    temp_path = ENV_PATH.with_suffix(".env.tmp")
    temp_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temp_path.replace(ENV_PATH)


def _apply_model_config(req: ModelConfigRequest, *, persist: bool) -> dict[str, str]:
    base_url = _validate_base_url(req.base_url)
    model = req.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model is required")
    api_key = req.api_key.strip() if req.api_key else config.LLM_API_KEY
    updates = {
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
        "LLM_TIMEOUT": str(req.timeout),
        "LLM_MAX_RETRIES": str(req.max_retries),
        "LLM_TEMPERATURE": str(req.temperature),
    }
    if req.api_key and req.api_key.strip():
        updates["LLM_API_KEY"] = req.api_key.strip()
    if persist:
        _write_env_updates(updates)
        for key, value in updates.items():
            os.environ[key] = value
        config.LLM_BASE_URL = base_url
        config.LLM_API_KEY = api_key
        config.LLM_MODEL = model
        config.DEFAULT_MODEL = model
        config.LLM_TIMEOUT = req.timeout
        config.LLM_MAX_RETRIES = req.max_retries
        config.LLM_TEMPERATURE = req.temperature
    return {"base_url": base_url, "api_key": api_key, "model": model}


def _validate_storage_dir(value: str, label: str) -> Path:
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"{label} must be an absolute path")
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=400, detail=f"{label} must be an existing directory")
    return path


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
    """取项目运行态。首次访问时从 run_log.jsonl 回填历史日志。"""
    job = JOBS.get(project_id)
    if job is not None:
        return job
    try:
        logs = run_log.read(_project_dir(project_id))
    except HTTPException:
        logs = []
    return JOBS.setdefault(
        project_id,
        {
            "running": False,
            "status": "idle",
            "message": logs[-1]["message"] if logs else "",
            "logs": logs,
            "updated_at": datetime.now().isoformat(),
        },
    )


def _log(project_id: str, message: str) -> None:
    job = _job(project_id)
    job["message"] = message
    job["updated_at"] = datetime.now().isoformat()
    try:
        entry = run_log.append(_project_dir(project_id), message)
    except HTTPException:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "message": message,
        }
    job["logs"].append({"time": entry["time"], "message": entry["message"]})
    job["logs"] = job["logs"][-run_log.MAX_ENTRIES:]
    _notify_subscribers(project_id)


def _notify_subscribers(project_id: str) -> None:
    """通知该项目的全部 SSE 连接有状态更新。

    每个连接的队列容量为 1：若消费者还没消费上一次更新，用最新的替换掉，
    避免运行中的项目日志洪泛导致队列堆积。真正的时间合并（coalescing）在
    `event_stream` 消费端完成：拿到第一个事件后进入短暂 drain 窗口，窗口内
    的后续事件合并为一次推送，避免前端每秒被刷数十次。
    """
    queues = SUBSCRIBERS.get(project_id)
    if not queues:
        return
    for queue in list(queues):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait("update")
        except asyncio.QueueFull:
            pass


def _artifact_paths(state: ProjectState) -> list[tuple[str, str, Path | None]]:
    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    artifacts: list[tuple[str, str, Path | None]] = [
        ("outline", "调研提纲", Path(state.outline_path) if state.outline_path else None),
        (
            "research_requirements",
            "研究需求清单",
            Path(state.research_plan_path)
            if state.research_plan_path
            else state.project_dir / config.FILE_RESEARCH_REQUIREMENTS,
        ),
        (
            "research_tasks",
            "结构化补研任务",
            config_tasks_path(state.project_dir),
        ),
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
            "chart_manifest",
            "图表清单",
            Path(state.chart_manifest_path)
            if state.chart_manifest_path
            else state.project_dir / config.FILE_CHART_MANIFEST,
        ),
        (
            "final_report_tex",
            "LaTeX 源文件",
            Path(state.final_report_tex_path)
            if state.final_report_tex_path
            else state.project_dir / FILE_FINAL_REPORT_TEX,
        ),
    ]
    if raw_dir.exists():
        for path in sorted(raw_dir.glob("round_*.md")):
            artifacts.append((path.stem, f"采集数据 {path.stem}", path))
        for path in sorted(raw_dir.glob("feedback_round_*.json")):
            artifacts.append((path.stem, f"验证反馈 {path.stem}", path))
        for path in sorted(raw_dir.glob("task_results_round_*.json")):
            artifacts.append((path.stem, f"任务回填 {path.stem}", path))
    return artifacts


def _read_artifact(path: Path | None) -> str:
    if not path or not path.exists() or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) > ARTIFACT_LIMIT:
        return text[:ARTIFACT_LIMIT] + "\n\n[... truncated]"
    return text


def _artifact_version(path: Path | None) -> str | None:
    """给产物一个稳定版本号（mtime + size），供前端跳过未变化的重复请求。

    用 mtime + size 而非全文 hash：大报告全文 hash 的 IO 成本不比直接读一遍
    低多少，mtime/size 足以表达"文件是否被重写"。缺失或不可读时返回 None。
    """
    if not path or not path.exists() or not path.is_file():
        return None
    try:
        stat = path.stat()
        return f"{stat.st_mtime_ns}-{stat.st_size}"
    except OSError:
        return None


async def _final_report_pdf_path(state: ProjectState) -> Path:
    final_report = Path(state.final_report_path) if state.final_report_path else None
    if not final_report or not final_report.exists():
        raise HTTPException(status_code=404, detail="最终报告还没有生成")
    pdf_path = state.project_dir / config.FILE_FINAL_REPORT_PDF
    manifest_path = state.project_dir / config.FILE_CHART_MANIFEST
    inputs = [final_report, manifest_path]
    if not pdf_path.exists() or any(
        path.is_file() and path.stat().st_mtime > pdf_path.stat().st_mtime
        for path in inputs
    ):
        try:
            artifacts = await generate_typeset_artifacts(
                topic=state.topic,
                project_dir=state.project_dir,
                final_report_path=final_report,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not artifacts["pdf_path"]:
            raise HTTPException(
                status_code=409,
                detail="已生成 HTML 与 LaTeX，但缺少配置的 LaTeX 引擎，无法生成正式 PDF",
            )
        pdf_path = artifacts["pdf_path"]
        state.chart_manifest_path = str(artifacts["manifest_path"])
        state.final_report_html_path = str(artifacts["html_path"])
        state.final_report_tex_path = str(artifacts["tex_path"])
        state.final_report_pdf_path = str(pdf_path)
        state.final_report_typeset_pdf_path = str(pdf_path)
        state.save()
    return pdf_path


def _checkpoint_file(state: ProjectState) -> dict[str, str] | None:
    """从统一的检查点规格派生前端所需的 key/title。"""
    spec = checkpoint_for(state.stage)
    return {"key": spec.key, "title": spec.title} if spec else None


def _research_plan_payload(state: ProjectState) -> dict[str, Any]:
    """需求清单在工作台的可见状态。缺失时前端显示迁移入口。"""
    plan = load_plan_or_none(state)
    if plan is None:
        canonical_outline = state.project_dir / config.FILE_OUTLINE
        referenced_outline = Path(state.outline_path) if state.outline_path else None
        has_outline = canonical_outline.is_file() or bool(
            referenced_outline and referenced_outline.is_file()
        )
        return {
            "available": False,
            "migration_required": (
                research_plan_migration_required(state) or has_outline
            ),
            "error": state.notes.get("research_plan_error"),
            "requirements": [],
            "coverage": state.notes.get("research_question_coverage", {}),
        }
    return {
        "available": True,
        "migration_required": False,
        "schema_version": plan.schema_version,
        "error": None,
        "warning": state.notes.get("research_plan_warning"),
        "requirements": [item.model_dump() for item in plan.requirements],
        "coverage": state.notes.get("research_question_coverage", {}),
    }


def _research_tasks_payload(state: ProjectState) -> dict[str, Any]:
    try:
        ledger = load_tasks_file(config_tasks_path(state.project_dir))
    except TasksError as exc:
        return {"available": False, "error": str(exc), "tasks": []}
    return {
        "available": True,
        "error": None,
        "tasks": [item.model_dump() for item in ledger.tasks],
        "counts": state.notes.get("research_task_counts", {}),
        "blocking_task_ids": state.notes.get("pending_critical_tasks", []),
    }


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
                "version": _artifact_version(path),
            }
        )

    failed = bool(state.failed_stage or state.last_error)
    blocked_reason = retry_blocked_reason(state)
    rerun_stages = available_rerun_stages(state)
    return {
        "id": project_id,
        "topic": state.topic,
        "stage": state.stage.value,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "project_dir": str(state.project_dir),
        "running": job["running"],
        "job_status": "error" if failed and not job["running"] else job["status"],
        "job_message": job["message"],
        "logs": job["logs"],
        "checkpoint": _checkpoint_file(state),
        "artifacts": artifacts,
        "collect_round": state.collect_round,
        "max_collect_rounds": state.max_collect_rounds,
        "converged": state.converged,
        "failed": failed,
        "failed_stage": state.failed_stage,
        "last_error": state.last_error,
        "retry_count": state.retry_count,
        "can_retry": blocked_reason is None and not job["running"],
        "retry_blocked_reason": blocked_reason if failed else None,
        "can_rerun": bool(rerun_stages) and not job["running"],
        "rerun_stages": [
            {"value": stage.value, "label": RERUN_STAGE_LABELS[stage]}
            for stage in rerun_stages
        ],
        "quality_gate": state.notes.get("quality_gate"),
        "quality_gate_reasons": state.notes.get("quality_gate_reasons", []),
        "research_plan": _research_plan_payload(state),
        "research_tasks": _research_tasks_payload(state),
        "clarification_questions": state.notes.get("clarification_questions", []),
        "clarification": state.clarification,
        "token_usage": state.token_usage or {},
    }


STRATEGIST_MAX_CLARIFY_ROUNDS = 3


def _clarification_transcript(state: ProjectState) -> str:
    if not state.clarification:
        return ""
    lines = [
        f"{index}. 问：{item['question']}\n   答：{item['answer']}"
        for index, item in enumerate(state.clarification, 1)
    ]
    return "\n\n## 已完成的需求澄清问答\n" + "\n".join(lines) + "\n"


async def _run_web_strategist(
    state: ProjectState, feedback: str | None = None
) -> StrategistOutcome:
    """Web 模式的 Agent1。

    与 CLI 的差异只在于对话方式：CLI 用 `Prompt.ask` 阻塞提问，Web 无法阻塞，
    因此给 Agent1 挂一个 `AskUser` 工具——调用它即表示需要用户回答，本次执行
    到此结束，状态机挂起到 `AWAIT_CLARIFICATION`。用户提交答案后重新执行，
    历史问答通过 prompt 回灌。

    澄清轮次达到上限后不再提供该工具，强制 Agent1 用默认值收敛。
    """
    outline_path = state.project_dir / config.FILE_OUTLINE
    state.project_dir.mkdir(parents=True, exist_ok=True)

    rounds_used = sum(1 for _ in state.clarification)
    allow_questions = (
        rounds_used < STRATEGIST_MAX_CLARIFY_ROUNDS * 3 and not feedback
    )
    pending: list[str] = []

    system_prompt = strategist._load_system_prompt()
    if allow_questions:
        system_prompt += (
            "\n\n## Web 模式要求\n"
            "- 你可以调用 `AskUser` 工具向用户提出关键澄清问题（一次最多 4 个，"
            "必须同时给出你的建议默认值）。调用后请立即结束本次回复，等待用户回答。\n"
            "- 只在信息缺失会实质影响调研方向时提问；能用合理默认值覆盖的不要问。\n"
            "- 如果已有信息足够，直接用 Write 工具生成提纲，不要提问。\n"
            f"- 澄清问答已进行 {rounds_used} 条，上限约 "
            f"{STRATEGIST_MAX_CLARIFY_ROUNDS * 3} 条。\n"
            f"- 调研主题：**{state.topic}**\n"
            f"- 提纲写入路径（必须严格使用）：`{outline_path}`\n"
        )
    else:
        system_prompt += (
            "\n\n## Web 模式要求\n"
            "- 不要再向用户追问；根据现有信息直接生成可审阅提纲。\n"
            "- 信息不足处使用合理默认值，并在提纲中标注“默认，可调整”。\n"
            f"- 调研主题：**{state.topic}**\n"
            f"- 提纲写入路径（必须严格使用）：`{outline_path}`\n"
        )
    system_prompt += _clarification_transcript(state)
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

    allowed_tools = ["Read", "Write"]
    registry = default_registry
    if allow_questions:
        registry = default_registry.subset(["Read", "Write"])

        async def ask_user(questions: list[str]) -> str:
            """Ask the user clarifying questions before drafting the outline."""
            cleaned = [str(item).strip() for item in questions if str(item).strip()]
            if not cleaned:
                return "Error: questions must not be empty."
            pending.extend(cleaned[:4])
            return "Questions delivered. Stop now and wait for the user's answers."

        registry.register(
            "AskUser",
            ask_user,
            {
                "name": "AskUser",
                "description": (
                    "Ask the user up to 4 clarifying questions about research scope, "
                    "goals, or focus. Each question should include your suggested default."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Clarifying questions, each with a suggested default.",
                        }
                    },
                    "required": ["questions"],
                },
            },
        )
        allowed_tools = ["Read", "Write", "AskUser"]

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=allowed_tools,
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
        await run_agent(prompt, options, client, registry)

    # 提纲优先：Agent1 若既提问又写了提纲，说明它已能收敛
    if outline_path.exists():
        return StrategistOutcome(outline_path=outline_path)
    if pending:
        return StrategistOutcome(questions=tuple(pending))
    raise RuntimeError("Agent1 未能生成调研提纲，也没有提出澄清问题。")


class WebPipelineHost:
    """Web 宿主：Agent1 单次生成提纲，检查点保存状态后挂起。

    检查点不阻塞事件循环——`resolve_checkpoint` 返回 PAUSE，状态机随即退出，
    由 `POST /api/projects/{id}/approval` 推进阶段并重新调度。
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    async def run_strategist(
        self, state: ProjectState, feedback: str | None
    ) -> StrategistOutcome:
        return await _run_web_strategist(state, feedback)

    async def resolve_clarification(
        self, state: ProjectState, questions: tuple[str, ...]
    ) -> list[str] | None:
        # 无法阻塞事件循环等输入：挂起，由 POST /clarification 提交答案后重新调度
        return None

    async def resolve_checkpoint(
        self, state: ProjectState, spec: CheckpointSpec
    ) -> CheckpointResult:
        return CheckpointResult(decision=CheckpointDecision.PAUSE)

    def log(self, message: str) -> None:
        _log(self.project_id, message)

    def announce_done(self, state: ProjectState) -> None:
        return None


async def _run_until_pause(project_id: str) -> None:
    lock = LOCKS.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        _log(project_id, "已有任务正在运行")
        return

    async with lock:
        job = _job(project_id)
        job["running"] = True
        job["status"] = "running"
        state: ProjectState | None = None
        try:
            state = _load_state(project_id)
            state.clear_failure()
            state.mark_runner()
            if recover_blocked_delivery(state):
                _log(project_id, "检测到旧的无证据交付状态，已回到采集验证阶段")
            await run_state_machine(state, WebPipelineHost(project_id))
            job["status"] = "idle"
        except ResearchPlanBlockedError as exc:
            # 与"证据不足"区分：需要用户先重新确认研究计划，补采无法解决。
            job["status"] = "error"
            _log(project_id, f"研究需求清单缺失，交付已阻断：{exc}")
        except DeliveryBlockedError as exc:
            job["status"] = "idle"
            _log(project_id, f"交付已暂停：{exc}")
        except Exception as exc:
            job["status"] = "error"
            if state is not None and not state.failed_stage:
                state.mark_failure(state.stage.value, str(exc))
            _log(project_id, f"运行失败：{exc}")
        finally:
            if state is not None:
                state.clear_runner()
            job["running"] = False
            job["updated_at"] = datetime.now().isoformat()
            _notify_subscribers(project_id)


def _schedule(project_id: str) -> None:
    """派发后台推进任务，并持有强引用直到任务结束。"""
    task = asyncio.create_task(_run_until_pause(project_id))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


def _recover_interrupted_projects() -> list[str]:
    """标记上次进程退出时停在 Agent 执行中的项目。

    判定依据是 `state.runner`：stage 处于运行态、且登记的推进进程已经不存在时，
    说明服务是在 Agent 跑到一半时退出的。标记为失败后，工作台会显示可重试，
    而不是静默停在"已暂停"。

    只看 stage 是不够的——服务重启的时机如果恰好撞上另一个进程正在推进的项目，
    会把健康的运行误标为中断（实测发生过）。`runner_is_alive` 在无法确定时保守
    返回 True，宁可漏标也不误标。
    """
    recovered: list[str] = []
    if not config.PROJECTS_DIR.exists():
        return recovered
    for path in config.PROJECTS_DIR.iterdir():
        if not path.is_dir() or not (path / config.FILE_STATE).exists():
            continue
        try:
            state = ProjectState.load(path)
        except Exception:
            continue
        if not state.stage.is_agent_running or state.failed_stage:
            continue
        if state.runner_is_alive:
            continue
        state.clear_runner()
        state.mark_failure(
            state.stage.value,
            "上次运行被中断（服务重启或进程退出），该阶段未完成。可点击重试从此阶段继续。",
        )
        _log(path.name, "检测到上次运行被中断，已标记为可重试")
        recovered.append(path.name)
    return recovered


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=HTML_HEADERS)


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "model": config.LLM_MODEL,
        "base_url": config.LLM_BASE_URL,
        "has_api_key": bool(config.LLM_API_KEY),
        "timeout": config.LLM_TIMEOUT,
        "max_retries": config.LLM_MAX_RETRIES,
        "temperature": config.LLM_TEMPERATURE,
        "search_provider": config.SEARCH_API_PROVIDER,
        "has_search_api_key": bool(config.SEARCH_API_KEY),
        "search_providers": sorted(SUPPORTED_PROVIDERS),
        "search_key_required": config.SEARCH_API_PROVIDER in {"serpapi", "tavily"},
        "embedding_model": os.getenv("SOURCE_EMBEDDING_MODEL", ""),
        "default_rounds": config.MAX_COLLECT_ROUNDS,
        "output_preference": config.OUTPUT_PREFERENCE,
        "projects_dir": str(config.PROJECTS_DIR),
        "source_data_dir": os.getenv("SOURCE_DATA_DIR", str(config.SOURCE_DATA_DIR)),
        "active_source_data_dir": str(config.SOURCE_DATA_DIR),
        "source_restart_required": Path(
            os.getenv("SOURCE_DATA_DIR", str(config.SOURCE_DATA_DIR))
        ).expanduser() != config.SOURCE_DATA_DIR,
    }


@app.put("/api/config/model")
async def api_update_model_config(req: ModelConfigRequest) -> dict[str, Any]:
    async with CONFIG_LOCK:
        _apply_model_config(req, persist=True)
    return await api_config()


@app.put("/api/config/workspace")
async def api_update_workspace_config(req: WorkspaceConfigRequest) -> dict[str, Any]:
    projects_dir = _validate_storage_dir(req.projects_dir, "Projects directory")
    source_data_dir = _validate_storage_dir(req.source_data_dir, "Source directory")
    updates = {
        "MAX_COLLECT_ROUNDS": str(req.default_rounds),
        "OUTPUT_PREFERENCE": req.output_preference,
        "PROJECTS_DIR": str(projects_dir),
        "SOURCE_DATA_DIR": str(source_data_dir),
    }
    async with CONFIG_LOCK:
        _write_env_updates(updates)
        for key, value in updates.items():
            os.environ[key] = value
        config.MAX_COLLECT_ROUNDS = req.default_rounds
        config.OUTPUT_PREFERENCE = req.output_preference
        config.PROJECTS_DIR = projects_dir
    return await api_config()


@app.put("/api/config/search")
async def api_update_search_config(req: SearchConfigRequest) -> dict[str, Any]:
    """保存搜索 provider 与 Key。AnySearch 的 Key 可选，serpapi/tavily 必填。"""
    needs_key = req.provider in {"serpapi", "tavily"}
    api_key = (req.api_key or "").strip()
    if needs_key and not api_key and not config.SEARCH_API_KEY:
        raise HTTPException(
            status_code=400, detail=f"{req.provider} 需要 SEARCH_API_KEY"
        )

    updates = {"SEARCH_API_PROVIDER": req.provider}
    if api_key:
        updates["SEARCH_API_KEY"] = api_key
    async with CONFIG_LOCK:
        _write_env_updates(updates)
        for key, value in updates.items():
            os.environ[key] = value
        config.SEARCH_API_PROVIDER = req.provider
        if api_key:
            config.SEARCH_API_KEY = api_key
    return await api_config()


@app.post("/api/config/search/test")
async def api_test_search_config() -> dict[str, Any]:
    """用当前配置发一次真实搜索，验证 provider 与 Key 是否可用。"""
    result = await web_search("Lumitrace search connectivity check", num_results=1)
    if result.startswith("Error:"):
        raise HTTPException(status_code=400, detail=result[len("Error:"):].strip())
    return {
        "ok": True,
        "provider": config.SEARCH_API_PROVIDER,
        "message": "搜索服务连接成功",
        "preview": result[:300],
    }


@app.post("/api/config/model/test")
async def api_test_model_config(req: ModelConfigRequest) -> dict[str, Any]:
    values = _apply_model_config(req, persist=False)
    if not values["api_key"]:
        raise HTTPException(status_code=400, detail="API Key is required")
    try:
        async with LLMClient(
            base_url=values["base_url"],
            api_key=values["api_key"],
            model=values["model"],
            timeout=min(req.timeout, 30),
            max_retries=1,
        ) as client:
            response = await client.chat(
                [ChatMessage(role="user", content="Reply with OK.")],
                temperature=0,
                max_tokens=4,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Connection failed: {exc}") from exc
    return {"ok": True, "message": "连接成功", "response": response.content or ""}


@app.get("/api/usage")
async def api_usage(days: int = 364) -> dict[str, Any]:
    """跨项目 token 用量汇总，供首页展示。"""
    days = max(7, min(days, 364))
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    project_dirs = [
        path
        for path in config.PROJECTS_DIR.iterdir()
        if path.is_dir() and (path / config.FILE_STATE).exists()
    ]
    summary = token_usage.aggregate(project_dirs, days=days)
    payload = summary.as_dict()
    payload["days"] = days
    payload["model"] = config.LLM_MODEL
    return payload


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
    state.max_collect_rounds = req.max_collect_rounds or config.MAX_COLLECT_ROUNDS
    state.notes["output_preference"] = req.output_preference or config.OUTPUT_PREFERENCE
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


@app.get("/api/projects/{project_id}/events")
async def api_project_events(project_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：项目状态或日志变化时推送，替代前端 3 秒全量轮询。

    每个连接维护一个容量为 1 的队列；连接断开（客户端关闭、任务取消）时
    自动从订阅集合移除。消费端做合并：拿到首个事件后进入一段 drain 窗口，
    窗口内连续到达的事件合并为一次 `update` 推送，降低高频日志下的推送次数。
    """
    _load_state(project_id)  # 校验项目存在，早失败返回 404

    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    SUBSCRIBERS.setdefault(project_id, set()).add(queue)

    # 合并窗口：两次推送之间的最小间隔（秒）。运行中的日志可能毫秒级到达，
    # 这里把窗口内的事件 coalesce 成一次推送，前端每 ~150ms 最多刷新一次。
    COALESCE_WINDOW = 0.15

    async def event_stream() -> AsyncIterator[str]:
        try:
            # 首次连接立即发一次，让前端拿到当前快照
            yield "event: update\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳，保持连接不被中间代理关闭
                    yield ": keep-alive\n\n"
                    continue
                # 合并窗口：尽可能清空这 150ms 内堆积的事件，合并为一次推送
                try:
                    while True:
                        await asyncio.wait_for(queue.get(), timeout=COALESCE_WINDOW)
                except asyncio.TimeoutError:
                    pass
                yield "event: update\ndata: {}\n\n"
        finally:
            SUBSCRIBERS.get(project_id, set()).discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/projects/{project_id}/clarification")
async def api_submit_clarification(
    project_id: str, req: ClarificationRequest
) -> dict[str, Any]:
    """提交 Agent1 澄清问题的回答，随后继续推进流水线。"""
    state = _load_state(project_id)
    job = _job(project_id)
    if job["running"]:
        raise HTTPException(status_code=409, detail="项目正在运行，请稍后再提交")
    if state.stage != Stage.AWAIT_CLARIFICATION:
        raise HTTPException(status_code=400, detail="项目当前没有待回答的澄清问题")

    questions = tuple(state.notes.get("clarification_questions", []))
    answers = [] if req.skip else [str(item) for item in req.answers]
    _append_clarification(state, questions, answers)
    state.advance_to(Stage.PLANNING)
    _log(
        project_id,
        "已跳过澄清问题，Agent1 将使用默认值"
        if req.skip
        else f"已提交 {len(questions)} 个澄清问题的回答",
    )
    _schedule(project_id)
    return _serialize_state(state)


@app.post("/api/projects/{project_id}/research-plan/migrate")
async def api_migrate_research_plan(project_id: str) -> dict[str, Any]:
    """旧项目迁移：从现有提纲重建研究需求清单。

    这是唯一的兼容入口，必须由用户显式触发——重建出的清单需要人工确认是否符合预期。
    """
    state = _load_state(project_id)
    job = _job(project_id)
    if job["running"]:
        raise HTTPException(status_code=409, detail="项目正在运行，请等待当前阶段结束")
    try:
        message = migrate_research_plan(state)
    except ResearchPlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _log(project_id, message)
    return {"ok": True, "message": message, "project": _serialize_state(state)}


@app.post("/api/projects/{project_id}/continue")
async def api_continue(project_id: str) -> dict[str, Any]:
    _load_state(project_id)
    _schedule(project_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/retry")
async def api_retry(project_id: str, req: RetryRequest | None = None) -> dict[str, Any]:
    """重试失败的项目：保留既有产物，仅复位失败阶段后继续推进。"""
    state = _load_state(project_id)
    job = _job(project_id)
    if job["running"]:
        raise HTTPException(status_code=409, detail="项目正在运行，无法重试")
    blocked = retry_blocked_reason(state)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)

    extra_rounds = req.extra_rounds if req else 1
    message = prepare_retry(state, extra_rounds=extra_rounds)
    job["status"] = "idle"
    _log(project_id, f"第 {state.retry_count} 次重试：{message}")
    _schedule(project_id)
    return {"ok": True, "message": message, "project": _serialize_state(state)}


@app.post("/api/projects/{project_id}/rerun")
async def api_rerun(project_id: str, req: RerunRequest) -> dict[str, Any]:
    """将项目回退到指定 Agent 阶段，备份旧产物后重新运行。"""
    state = _load_state(project_id)
    job = _job(project_id)
    if job["running"]:
        raise HTTPException(status_code=409, detail="项目正在运行，无法回退重跑")

    try:
        message = prepare_stage_rerun(state, Stage(req.stage))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job["status"] = "idle"
    _log(project_id, message)
    _schedule(project_id)
    return {"ok": True, "message": message, "project": _serialize_state(state)}


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str) -> dict[str, Any]:
    """删除项目目录及其全部产物。运行中的项目不允许删除。"""
    _load_state(project_id)
    job = _job(project_id)
    if job["running"]:
        raise HTTPException(status_code=409, detail="项目正在运行，请先等待当前阶段结束")

    # 只删除请求的项目目录本身（_project_dir 已校验必须位于 PROJECTS_DIR 下）
    project_dir = _project_dir(project_id)
    if project_dir.resolve() == config.PROJECTS_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid project id")

    shutil.rmtree(project_dir)
    JOBS.pop(project_id, None)
    LOCKS.pop(project_id, None)
    SUBSCRIBERS.pop(project_id, None)
    return {"ok": True, "id": project_id}


@app.post("/api/projects/{project_id}/approval")
async def api_approval(project_id: str, req: ApprovalRequest) -> dict[str, Any]:
    state = _load_state(project_id)
    feedback = req.feedback.strip() or "请重新优化"
    # 审批本身就是用户显式动作，先清掉旧的失败标记，避免残留“失败”状态
    state.clear_failure()

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
            try:
                _assert_delivery_ready(state)
            except PipelineError as exc:
                _log(project_id, str(exc))
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
async def api_artifact(
    project_id: str,
    artifact_key: str,
    response: Response,
) -> dict[str, Any]:
    response.headers.update(HTML_HEADERS)
    state = _load_state(project_id)
    for key, label, path in _artifact_paths(state):
        if key == artifact_key:
            payload = {
                "key": key,
                "label": label,
                "exists": bool(path and path.exists()),
                "content": _read_artifact(path),
                "name": path.name if path else "",
                "version": _artifact_version(path),
            }
            if key == "final_report":
                html_path = (
                    Path(state.final_report_html_path)
                    if state.final_report_html_path
                    else state.project_dir / config.FILE_FINAL_REPORT_HTML
                )
                if html_path.is_file():
                    payload["html"] = _read_artifact(html_path)
            return payload
    raise HTTPException(status_code=404, detail="Artifact not found")


@app.get("/api/projects/{project_id}/charts/{chart_file}")
async def api_report_chart(project_id: str, chart_file: str) -> FileResponse:
    _load_state(project_id)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}\.(?:svg|png)", chart_file):
        raise HTTPException(status_code=400, detail="Invalid chart file")
    path = _project_dir(project_id) / "05_charts" / chart_file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Chart not found")
    media_type = "image/svg+xml" if path.suffix == ".svg" else "image/png"
    return FileResponse(path, media_type=media_type, headers=HTML_HEADERS)


@app.get("/api/projects/{project_id}/download/final-report.pdf")
async def api_download_final_report_pdf(project_id: str) -> FileResponse:
    state = _load_state(project_id)
    pdf_path = await _final_report_pdf_path(state)
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
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    state.final_report_tex_path = str(artifacts["tex_path"])
    state.chart_manifest_path = str(artifacts["manifest_path"])
    state.final_report_html_path = str(artifacts["html_path"])
    if artifacts["pdf_path"]:
        state.final_report_pdf_path = str(artifacts["pdf_path"])
        state.final_report_typeset_pdf_path = str(artifacts["pdf_path"])
        # 手动重排版成功即证明排版问题已消除；清掉上次的排版失败标记，
        # 否则项目会一直显示"运行失败"，用户无从判断是否已修好。
        state.notes.pop("latex_typeset_error", None)
        if state.failed_stage and "排版" in state.failed_stage:
            state.failed_stage = None
            state.last_error = None
    state.save()

    return {
        "status": "pdf" if artifacts["pdf_path"] else "tex_only",
        "message": "已生成正式券商研报 PDF" if artifacts["pdf_path"] else "已生成 HTML 与 LaTeX；本机缺少配置的 LaTeX 引擎，暂未编译 PDF",
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
        state.chart_manifest_path = str(artifacts["manifest_path"])
        state.final_report_html_path = str(artifacts["html_path"])
        state.final_report_tex_path = str(tex_path)
        if artifacts["pdf_path"]:
            state.final_report_pdf_path = str(artifacts["pdf_path"])
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
    pdf_path = await _final_report_pdf_path(state)
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{project_id}_final_report.pdf",
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
    parser.add_argument(
        "--allow-insecure-host",
        action="store_true",
        help="允许在未设置 WEB_AUTH_TOKEN 的情况下绑定非回环地址（不推荐）",
    )
    args = parser.parse_args()

    loopback = _is_loopback(args.host)
    if config.WEB_AUTH_TOKEN:
        print(
            "[lumitrace] 已启用访问令牌校验。"
            f"首次访问请带上 ?token=…，或在请求中携带 {AUTH_HEADER} 头。"
        )
    elif not loopback and not args.allow_insecure_host:
        raise SystemExit(
            f"拒绝启动：--host {args.host} 会把工作台暴露到网络，但未设置 WEB_AUTH_TOKEN。\n"
            "任何能访问该地址的人都可以读取全部调研数据、修改模型配置（含写入 .env）、删除项目。\n"
            "请任选其一：\n"
            "  1) 在 .env 设置 WEB_AUTH_TOKEN=<足够长的随机串>\n"
            "  2) 改用 --host 127.0.0.1 只监听本机\n"
            "  3) 确认风险后追加 --allow-insecure-host"
        )
    elif not loopback:
        print(
            f"[lumitrace] 警告：正在无认证暴露到 {args.host}，"
            "任何能访问该地址的人都可读写本工作台的全部数据。"
        )

    uvicorn.run(
        "research_agent.web_app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
