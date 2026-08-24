import asyncio
from pathlib import Path

import httpx
import pytest

from research_agent import config
from research_agent import orchestrator, run_log, web_app
from research_agent.agents import validator
from research_agent.research_plan import derive_plan_from_outline, save_plan
from research_agent.state import ProjectState, Stage


def fix_plan(state: ProjectState, *question_ids: str) -> None:
    """写入研究开始阶段固定的需求清单（R1 后所有门禁都读取它）。"""
    ids = question_ids or ("q1",)
    outline_text = (
        f"# 《{state.topic}》调研提纲\n\n## 二、核心研究问题\n"
        + "\n".join(
            f"{index}. 研究问题 {value}" for index, value in enumerate(ids, 1)
        )
        + "\n"
    )
    outline = state.project_dir / config.FILE_OUTLINE
    outline.parent.mkdir(parents=True, exist_ok=True)
    outline.write_text(outline_text, encoding="utf-8")
    state.outline_path = str(outline)
    plan, _ = derive_plan_from_outline(state.topic, outline_text)
    for item, question_id in zip(plan.requirements, ids, strict=True):
        item.question_id = question_id
    save_plan(state, plan)


@pytest.mark.anyio
async def test_model_config_can_be_saved_without_exposing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("SEARCH_API_PROVIDER=duckduckgo\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MODEL", "gpt-4o")
    monkeypatch.setattr(config, "DEFAULT_MODEL", "gpt-4o")
    monkeypatch.setattr(config, "LLM_TIMEOUT", 120.0)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "LLM_TEMPERATURE", 0.7)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_TIMEOUT", "120")
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/model",
            json={
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-secret-key",
                "model": "deepseek-chat",
                "timeout": 60,
                "max_retries": 2,
                "temperature": 0.4,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "deepseek-chat"
    assert payload["has_api_key"] is True
    assert "api_key" not in payload
    saved = env_path.read_text(encoding="utf-8")
    assert "LLM_API_KEY=test-secret-key" in saved
    assert "SEARCH_API_PROVIDER=duckduckgo" in saved


@pytest.mark.anyio
async def test_model_config_rejects_non_http_base_url() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/model",
            json={
                "base_url": "file:///tmp/model",
                "model": "local-model",
            },
        )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_workspace_config_can_be_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    projects_dir = tmp_path / "projects"
    source_dir = tmp_path / "sources"
    projects_dir.mkdir()
    source_dir.mkdir()
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "MAX_COLLECT_ROUNDS", 3)
    monkeypatch.setattr(config, "OUTPUT_PREFERENCE", "balanced")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "old-projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "active-sources")
    monkeypatch.delenv("SOURCE_DATA_DIR", raising=False)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/workspace",
            json={
                "default_rounds": 5,
                "output_preference": "deep",
                "projects_dir": str(projects_dir),
                "source_data_dir": str(source_dir),
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["default_rounds"] == 5
    assert payload["output_preference"] == "deep"
    assert payload["projects_dir"] == str(projects_dir)
    assert payload["source_data_dir"] == str(source_dir)
    assert payload["source_restart_required"] is True
    saved = env_path.read_text(encoding="utf-8")
    assert "MAX_COLLECT_ROUNDS=5" in saved
    assert "OUTPUT_PREFERENCE=deep" in saved
    assert f"PROJECTS_DIR={projects_dir}" in saved
    assert f"SOURCE_DATA_DIR={source_dir}" in saved


@pytest.mark.anyio
async def test_workspace_config_rejects_relative_paths() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/workspace",
            json={
                "default_rounds": 3,
                "output_preference": "balanced",
                "projects_dir": "projects",
                "source_data_dir": ".data/sources",
            },
        )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_final_source_approval_is_blocked_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="blocked",
        date_str="20260720",
        stage=Stage.AWAIT_FINAL_SOURCE_APPROVAL,
    )
    state.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/approval",
            json={"approved": True},
        )

    assert response.status_code == 409
    assert ProjectState.load(state.project_dir).stage == Stage.AWAIT_FINAL_SOURCE_APPROVAL


@pytest.mark.anyio
async def test_web_delivery_gate_pauses_instead_of_reporting_agent_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="blocked", date_str="20260720", stage=Stage.FORMATTING)
    state.save()
    fix_plan(state, "q1")

    await web_app._run_until_pause(state.project_dir.name)

    job = web_app.JOBS[state.project_dir.name]
    assert job["status"] == "idle"
    assert job["message"].startswith("交付已暂停：")
    assert "Agent5 未启动，本错误不会重试" in job["message"]


@pytest.mark.anyio
async def test_web_blocks_legacy_project_without_research_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 与 CLI 共用同一道需求清单门禁：缺清单时不静默放行，并给出迁移入口。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="legacy-web", date_str="20260730", stage=Stage.FORMATTING)
    state.project_dir.mkdir(parents=True, exist_ok=True)
    outline = state.project_dir / config.FILE_OUTLINE
    outline.write_text(
        "# 提纲\n\n## 二、核心研究问题\n1. 市场规模有多大？\n2. 竞争格局如何？\n",
        encoding="utf-8",
    )
    state.outline_path = str(outline)
    state.save()
    project_id = state.project_dir.name

    await web_app._run_until_pause(project_id)

    job = web_app.JOBS[project_id]
    assert job["status"] == "error"
    assert "研究需求清单缺失" in job["message"]

    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blocked = await client.get(f"/api/projects/{project_id}")
        migrated = await client.post(
            f"/api/projects/{project_id}/research-plan/migrate"
        )
        after = await client.get(f"/api/projects/{project_id}")

    blocked_payload = blocked.json()
    assert blocked_payload["research_plan"]["available"] is False
    assert blocked_payload["research_plan"]["migration_required"] is True
    # 需求清单缺失时重试无意义，必须先迁移
    assert blocked_payload["can_retry"] is False
    assert "migrate-plan" in blocked_payload["retry_blocked_reason"]

    assert migrated.status_code == 200
    assert "2 个研究问题" in migrated.json()["message"]
    after_payload = after.json()
    assert after_payload["research_plan"]["available"] is True
    assert len(after_payload["research_plan"]["requirements"]) == 2


def test_workspace_hides_plan_migration_before_outline_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(
        topic="new-project",
        date_str="20260817",
        stage=Stage.AWAIT_CLARIFICATION,
    )
    state.save()

    payload = web_app._research_plan_payload(state)

    assert payload["available"] is False
    assert payload["migration_required"] is False


def test_workspace_offers_plan_migration_for_legacy_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(
        topic="legacy-project",
        date_str="20260719",
        stage=Stage.DONE,
    )
    outline = state.project_dir / config.FILE_OUTLINE
    outline.parent.mkdir(parents=True)
    outline.write_text(
        "# 提纲\n\n## 二、核心研究问题\n1. 市场规模是多少？\n",
        encoding="utf-8",
    )
    state.outline_path = str(outline)
    state.save()

    payload = web_app._research_plan_payload(state)

    assert payload["available"] is False
    assert payload["migration_required"] is True


def test_workspace_plan_migration_panel_requires_explicit_flag() -> None:
    source = (web_app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "plan.migration_required === true" in source


def test_workspace_places_progress_then_evaluation_before_run_panels() -> None:
    source = (web_app.STATIC_DIR / "workspace.html").read_text(encoding="utf-8")

    plan_position = source.index('id="planPanel"')
    pipeline_position = source.index('id="pipeline"')
    timeline_position = source.index("Agent 执行时间线")
    artifacts_position = source.index("<h2>项目产物</h2>")

    assert pipeline_position < plan_position < timeline_position < artifacts_position
    assert 'id="workspaceContent" class="workspace-shell hidden"' in source
    assert '<details id="rerunPanel"' in source


def test_workspace_long_timeline_content_does_not_collapse_column_gap() -> None:
    styles = (web_app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".workspace-grid > *, .workspace-stack, .workspace-stack > * { min-width: 0; }" in styles
    assert "grid-template-columns: 64px minmax(0, 1fr) auto" in styles
    assert ".timeline-row p { min-width: 0;" in styles
    assert "overflow-wrap: anywhere" in styles


def test_results_sidebar_items_fill_the_available_width() -> None:
    styles = (web_app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    template = (web_app.STATIC_DIR / "results.html").read_text(encoding="utf-8")

    assert ".result-item { display: grid; width: 100%;" in styles
    assert f"styles.css?v={web_app.STATIC_VERSION}" in template


def test_usage_heatmap_has_precise_hover_tooltip() -> None:
    source = (web_app.STATIC_DIR / "home.js").read_text(encoding="utf-8")
    styles = (web_app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    template = (web_app.STATIC_DIR / "research.html").read_text(encoding="utf-8")

    assert 'id="usageTooltip"' in template
    assert "data-usage-tooltip" in source
    assert 'toLocaleString("zh-CN")' in source
    assert 'data-tooltip-value="${formatExactTokens(value)}"' in source
    assert 'heatmap.addEventListener("pointerover"' in source
    assert "document.body.appendChild(tooltip)" in source
    assert ".usage-tooltip.visible" in styles


@pytest.mark.anyio
async def test_web_migration_does_not_overwrite_valid_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 迁移入口与 CLI 一样，只允许修复缺失或失效的清单。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="valid-plan", date_str="20260730", stage=Stage.FORMATTING)
    state.save()
    fix_plan(state, "q1")
    plan_path = state.project_dir / config.FILE_RESEARCH_REQUIREMENTS
    before = plan_path.read_bytes()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/research-plan/migrate"
        )

    assert response.status_code == 400
    assert "现有清单未被覆盖" in response.json()["detail"]
    assert plan_path.read_bytes() == before


@pytest.mark.anyio
async def test_failed_project_can_be_retried_and_keeps_existing_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="retryable",
        date_str="20260728",
        stage=Stage.COLLECTING_AND_VALIDATING,
    )
    state.collect_round = 3
    state.max_collect_rounds = 3
    state.outline_path = str(state.project_dir / config.FILE_OUTLINE)
    state.save()
    Path(state.outline_path).write_text("# outline", encoding="utf-8")
    state.mark_failure("Agent3·证据审查（第3轮）", "证据质量门槛未通过")

    scheduled: list[str] = []
    monkeypatch.setattr(web_app, "_schedule", lambda project_id: scheduled.append(project_id))

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get(f"/api/projects/{state.project_dir.name}")
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/retry",
            json={"extra_rounds": 1},
        )

    assert before.status_code == 200
    assert before.json()["failed"] is True
    assert before.json()["can_retry"] is True
    assert response.status_code == 200

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.failed_stage is None
    assert reloaded.last_error is None
    assert reloaded.retry_count == 1
    assert reloaded.max_collect_rounds == 4
    assert reloaded.collect_round == 3  # 既有采集轮次保留
    assert Path(reloaded.outline_path).exists()  # 既有产物保留
    assert scheduled == [state.project_dir.name]


@pytest.mark.anyio
async def test_retry_rewinds_delivery_blocked_project_to_collecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="blocked-delivery", date_str="20260728", stage=Stage.FORMATTING)
    state.notes["delivery_blocked_stage"] = "evidence"
    state.save()
    state.mark_failure("交付证据门槛", "blocked: no evidence")

    monkeypatch.setattr(web_app, "_schedule", lambda project_id: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/projects/{state.project_dir.name}/retry", json={})

    assert response.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.COLLECTING_AND_VALIDATING
    assert reloaded.converged is False
    assert reloaded.collect_round == 0
    assert reloaded.failed_stage is None


@pytest.mark.anyio
async def test_retry_rejected_for_healthy_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="healthy", date_str="20260728", stage=Stage.SOURCING)
    state.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/projects/{state.project_dir.name}/retry", json={})

    assert response.status_code == 400


@pytest.mark.anyio
async def test_completed_project_can_rerun_from_selected_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="completed-rerun", date_str="20260731", stage=Stage.DONE)
    state.save()
    outline = state.project_dir / config.FILE_OUTLINE
    sources_final = state.project_dir / config.FILE_SOURCES_FINAL
    analysis = state.project_dir / config.FILE_ANALYSIS
    final_report = state.project_dir / config.FILE_FINAL_REPORT
    for path in (outline, sources_final, analysis, final_report):
        path.write_text(path.name, encoding="utf-8")
    state.outline_path = str(outline)
    state.sources_final_path = str(sources_final)
    state.analysis_path = str(analysis)
    state.final_report_path = str(final_report)
    state.save()
    project_id = state.project_dir.name

    scheduled: list[str] = []
    monkeypatch.setattr(web_app, "_schedule", lambda value: scheduled.append(value))
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = await client.get(f"/api/projects/{project_id}")
        response = await client.post(
            f"/api/projects/{project_id}/rerun",
            json={"stage": "analyzing"},
        )

    assert before.json()["can_rerun"] is True
    assert [item["value"] for item in before.json()["rerun_stages"]] == [
        "planning",
        "sourcing",
        "collecting_and_validating",
        "analyzing",
        "formatting",
    ]
    assert response.status_code == 200
    assert response.json()["project"]["stage"] == "analyzing"
    assert scheduled == [project_id]
    assert ProjectState.load(state.project_dir).stage == Stage.ANALYZING


@pytest.mark.anyio
async def test_running_project_cannot_be_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="running-rerun", date_str="20260731", stage=Stage.DONE)
    state.save()
    web_app._job(state.project_dir.name)["running"] = True

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/rerun",
            json={"stage": "planning"},
        )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_failed_project_can_be_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="deletable", date_str="20260728", stage=Stage.PLANNING)
    state.save()
    state.mark_failure("Agent1·战略规划", "boom")
    project_id = state.project_dir.name

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request("DELETE", f"/api/projects/{project_id}")
        listing = await client.get("/api/projects")

    assert response.status_code == 200
    assert not state.project_dir.exists()
    assert project_id not in [item["id"] for item in listing.json()["projects"]]
    assert project_id not in web_app.JOBS


@pytest.mark.anyio
async def test_running_project_cannot_be_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="busy", date_str="20260728", stage=Stage.PLANNING)
    state.save()
    web_app._job(state.project_dir.name)["running"] = True

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request("DELETE", f"/api/projects/{state.project_dir.name}")

    assert response.status_code == 409
    assert state.project_dir.exists()


@pytest.mark.anyio
async def test_delete_rejects_unknown_project() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request("DELETE", "/api/projects/does-not-exist")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_retry_stays_available_past_soft_round_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="soft-limit",
        date_str="20260728",
        stage=Stage.COLLECTING_AND_VALIDATING,
    )
    state.collect_round = orchestrator.RETRY_ROUND_SOFT_LIMIT
    state.max_collect_rounds = orchestrator.RETRY_ROUND_SOFT_LIMIT
    state.save()
    state.mark_failure("Agent3·证据审查", "quality gate failed")
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(f"/api/projects/{state.project_dir.name}")
        response = await client.post(f"/api/projects/{state.project_dir.name}/retry", json={})

    assert detail.json()["can_retry"] is True
    assert detail.json()["retry_blocked_reason"] is None
    assert response.status_code == 200
    assert "开销" in response.json()["message"]


@pytest.mark.anyio
async def test_agent_stage_failure_is_surfaced_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="agent-failure", date_str="20260728", stage=Stage.PLANNING)
    state.save()
    project_id = state.project_dir.name

    attempts: list[int] = []

    async def failing_strategist(_state: ProjectState, _feedback=None) -> Path:
        attempts.append(1)
        raise RuntimeError("模型连接失败")

    monkeypatch.setattr(web_app, "_run_web_strategist", failing_strategist)

    await web_app._run_until_pause(project_id)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed_view = await client.get(f"/api/projects/{project_id}")

    payload = failed_view.json()
    assert payload["failed"] is True
    assert payload["can_retry"] is True
    assert payload["job_status"] == "error"
    assert "模型连接失败" in payload["last_error"]
    assert ProjectState.load(state.project_dir).failed_stage == "Agent1·战略规划"

    outline = state.project_dir / config.FILE_OUTLINE

    async def working_strategist(_state: ProjectState, _feedback=None) -> Path:
        outline.parent.mkdir(parents=True, exist_ok=True)
        outline.write_text(
            "# 调研提纲\n\n## 二、核心研究问题\n1. 市场规模是多少？\n",
            encoding="utf-8",
        )
        return outline

    monkeypatch.setattr(web_app, "_run_web_strategist", working_strategist)
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        retried = await client.post(f"/api/projects/{project_id}/retry", json={})

    assert retried.status_code == 200
    await web_app._run_until_pause(project_id)

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.AWAIT_OUTLINE_APPROVAL
    assert reloaded.failed_stage is None
    assert reloaded.retry_count == 1
    assert outline.exists()


@pytest.mark.anyio
async def test_blocked_final_approval_becomes_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="approval-blocked",
        date_str="20260728",
        stage=Stage.AWAIT_FINAL_SOURCE_APPROVAL,
    )
    state.save()
    fix_plan(state, "q1")
    project_id = state.project_dir.name
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blocked = await client.post(
            f"/api/projects/{project_id}/approval", json={"approved": True}
        )
        detail = await client.get(f"/api/projects/{project_id}")
        retried = await client.post(f"/api/projects/{project_id}/retry", json={})

    assert blocked.status_code == 409
    assert detail.json()["failed"] is True
    assert detail.json()["can_retry"] is True
    assert retried.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.COLLECTING_AND_VALIDATING
    assert reloaded.failed_stage is None


@pytest.mark.anyio
async def test_quality_gate_failure_marks_project_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查未通过后项目不再直接失败退出，而是进入可重试状态并追加轮次。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="gate-failure",
        date_str="20260728",
        stage=Stage.COLLECTING_AND_VALIDATING,
    )
    state.max_collect_rounds = 1
    state.outline_path = str(state.project_dir / config.FILE_OUTLINE)
    state.sources_draft_path = str(state.project_dir / config.FILE_SOURCES_DRAFT)
    state.save()
    fix_plan(state, "q1")
    project_id = state.project_dir.name

    raw_dir = state.project_dir / config.FILE_RAW_DATA_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)

    collected: list[int] = []

    async def fake_collection(_state, round_idx, feedback_path=None):
        collected.append(round_idx)
        path = raw_dir / config.FILE_RAW_ROUND.format(n=round_idx)
        path.write_text(f"round {round_idx}", encoding="utf-8")
        return path

    async def fake_validation(_state, round_idx, raw_path):
        path = raw_dir / config.FILE_FEEDBACK_ROUND.format(n=round_idx)
        feedback = validator.ValidationFeedback(round=round_idx, converged=False)
        path.write_text(feedback.model_dump_json(), encoding="utf-8")
        return path, feedback

    monkeypatch.setattr(orchestrator.collector, "run_collection_round", fake_collection)
    monkeypatch.setattr(orchestrator.validator, "run_validation", fake_validation)

    await web_app._run_until_pause(project_id)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(f"/api/projects/{project_id}")
        monkeypatch.setattr(web_app, "_schedule", lambda pid: None)
        retried = await client.post(f"/api/projects/{project_id}/retry", json={"extra_rounds": 1})

    payload = detail.json()
    assert collected == [1]
    assert payload["failed"] is True
    assert payload["can_retry"] is True
    assert "证据质量门槛未通过" in payload["last_error"]
    assert retried.status_code == 200

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.max_collect_rounds == 2
    assert reloaded.collect_round == 1
    assert (raw_dir / config.FILE_RAW_ROUND.format(n=1)).exists()

    await web_app._run_until_pause(project_id)
    assert collected == [1, 2]


@pytest.mark.anyio
async def test_delete_rejects_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / config.FILE_STATE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECTS_DIR", projects_dir)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request("DELETE", "/api/projects/..%2Foutside")

    assert response.status_code in {400, 404}
    assert outside.exists()


@pytest.mark.anyio
async def test_rejecting_blocked_final_sources_clears_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驳回也是一种恢复路径，不应留下“失败”状态。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="reject-after-block",
        date_str="20260728",
        stage=Stage.AWAIT_FINAL_SOURCE_APPROVAL,
    )
    state.save()
    state.mark_failure("交付证据门槛", "blocked")
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/approval",
            json={"approved": False, "feedback": "补充官方年报"},
        )

    assert response.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.COLLECTING_AND_VALIDATING
    assert reloaded.failed_stage is None
    assert response.json()["failed"] is False


@pytest.mark.anyio
async def test_web_host_pauses_at_checkpoints_and_shares_one_state_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 不再有独立状态机：走 orchestrator.run_state_machine + WebPipelineHost。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="web-host", date_str="20260728")
    state.save()
    project_id = state.project_dir.name

    outline = state.project_dir / config.FILE_OUTLINE

    async def fake_strategist(target: ProjectState, feedback=None) -> Path:
        outline.write_text(
            "# 调研提纲\n\n## 二、核心研究问题\n1. 市场规模是多少？\n",
            encoding="utf-8",
        )
        return outline

    monkeypatch.setattr(web_app, "_run_web_strategist", fake_strategist)

    await web_app._run_until_pause(project_id)

    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.AWAIT_OUTLINE_APPROVAL
    assert web_app.JOBS[project_id]["status"] == "idle"
    assert web_app.JOBS[project_id]["running"] is False

    # 状态机唯一实现在 orchestrator；web_app 不应再持有一份
    assert not hasattr(web_app, "_run_state_machine")

    host = web_app.WebPipelineHost(project_id)
    spec = orchestrator.checkpoint_for(Stage.AWAIT_OUTLINE_APPROVAL)
    result = await host.resolve_checkpoint(reloaded, spec)
    assert result.decision is orchestrator.CheckpointDecision.PAUSE


@pytest.mark.anyio
async def test_checkpoint_payload_is_derived_from_shared_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/projects 的 checkpoint 字段来自统一规格，不再硬编码。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()

    transport = httpx.ASGITransport(app=web_app.app)
    for spec in orchestrator.CHECKPOINT_SPECS:
        state = ProjectState(
            topic=f"cp-{spec.key}", date_str="20260728", stage=spec.stage
        )
        state.save()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/api/projects/{state.project_dir.name}")
        assert response.json()["checkpoint"] == {"key": spec.key, "title": spec.title}


@pytest.mark.anyio
async def test_logs_survive_service_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """执行日志落盘到 run_log.jsonl，清空内存 JOBS 后仍可读回（backlog 第 3 项）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(topic="log-persist", date_str="20260728")
    state.save()
    project_id = state.project_dir.name

    web_app._log(project_id, "Agent1 正在生成调研提纲")
    web_app._log(project_id, "调研提纲已生成，等待审批")

    assert run_log.log_path(state.project_dir).is_file()

    # 模拟服务重启：内存态全部丢弃
    web_app.JOBS.clear()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/projects/{project_id}")

    payload = response.json()
    messages = [item["message"] for item in payload["logs"]]
    assert messages == ["Agent1 正在生成调研提纲", "调研提纲已生成，等待审批"]
    assert payload["job_message"] == "调研提纲已生成，等待审批"


@pytest.mark.anyio
async def test_interrupted_project_is_marked_retryable_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停在 Agent 运行态的项目在启动扫描后变为可重试（backlog 第 2 项）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()

    interrupted = ProjectState(
        topic="interrupted", date_str="20260728", stage=Stage.COLLECTING_AND_VALIDATING
    )
    interrupted.save()
    waiting = ProjectState(
        topic="waiting", date_str="20260728", stage=Stage.AWAIT_OUTLINE_APPROVAL
    )
    waiting.save()
    finished = ProjectState(topic="finished", date_str="20260728", stage=Stage.DONE)
    finished.save()

    recovered = web_app._recover_interrupted_projects()

    assert recovered == [interrupted.project_dir.name]
    reloaded = ProjectState.load(interrupted.project_dir)
    assert reloaded.failed_stage == Stage.COLLECTING_AND_VALIDATING.value
    assert "上次运行被中断" in reloaded.last_error
    # 检查点与已完成项目不受影响
    assert ProjectState.load(waiting.project_dir).failed_stage is None
    assert ProjectState.load(finished.project_dir).failed_stage is None


@pytest.mark.anyio
async def test_startup_recovery_keeps_existing_failure_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有失败原因的项目不被启动扫描覆盖。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(topic="already-failed", date_str="20260728", stage=Stage.ANALYZING)
    state.save()
    state.mark_failure("Agent4·深度分析", "LLM timeout")

    assert web_app._recover_interrupted_projects() == []
    assert ProjectState.load(state.project_dir).last_error == "LLM timeout"


@pytest.mark.anyio
async def test_startup_recovery_leaves_live_runner_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一个进程正在推进的项目不得被标记为中断。

    只看 stage 会把健康的运行误标为中断：服务重启的时机若撞上 CLI 或另一个
    worker 正在跑的项目，用户会在 Agent 正常工作时看到"已中断，请重试"。
    """
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    running = ProjectState(
        topic="live-runner", date_str="20260817", stage=Stage.ANALYZING
    )
    running.save()
    running.mark_runner()  # 登记当前进程——它显然活着

    assert web_app._recover_interrupted_projects() == []
    reloaded = ProjectState.load(running.project_dir)
    assert reloaded.failed_stage is None
    assert reloaded.runner is not None


@pytest.mark.anyio
async def test_startup_recovery_marks_dead_runner_as_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记的推进进程已不存在时，项目才算被中断。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    dead = ProjectState(topic="dead-runner", date_str="20260817", stage=Stage.FORMATTING)
    dead.save()
    dead.mark_runner()
    # 伪造一个几乎不可能存在的 PID，保留当前 boot_id 以确认判定走的是存活检查
    dead.runner = {**dead.runner, "pid": 2**22 - 1}
    dead.save()

    assert web_app._recover_interrupted_projects() == [dead.project_dir.name]
    reloaded = ProjectState.load(dead.project_dir)
    assert reloaded.failed_stage == Stage.FORMATTING.value
    assert reloaded.runner is None


def test_runner_from_previous_boot_is_not_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主机重启后 PID 会被复用，旧 boot_id 的登记必须一律失效。

    否则一个无关的新进程会被误判为"仍在推进"，真正被中断的项目永远不会被标记
    为可重试。
    """
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="stale-boot", date_str="20260817", stage=Stage.ANALYZING)
    state.save()
    state.mark_runner()
    assert state.runner_is_alive is True

    state.runner = {**state.runner, "boot_id": "boot-from-a-previous-uptime"}
    assert state.runner_is_alive is False


@pytest.mark.anyio
async def test_scheduled_background_task_is_strongly_referenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_schedule 必须持有任务强引用，否则任务可能被 GC 回收（backlog 第 2 项）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    web_app.BACKGROUND_TASKS.clear()
    state = ProjectState(topic="task-ref", date_str="20260728", stage=Stage.DONE)
    state.save()

    web_app._schedule(state.project_dir.name)
    assert len(web_app.BACKGROUND_TASKS) == 1

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # 任务完成后回调应把引用摘除，避免集合无限增长
    assert web_app.BACKGROUND_TASKS == set()


@pytest.mark.anyio
async def test_search_config_can_be_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搜索 provider 与 Key 可持久化（backlog 第 4 项）。"""
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=gpt-4o\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "duckduckgo")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/search",
            json={"provider": "tavily", "api_key": "tv-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["search_provider"] == "tavily"
    assert payload["has_search_api_key"] is True
    assert payload["search_key_required"] is True
    assert "api_key" not in payload
    saved = env_path.read_text(encoding="utf-8")
    assert "SEARCH_API_PROVIDER=tavily" in saved
    assert "SEARCH_API_KEY=tv-secret" in saved
    assert "LLM_MODEL=gpt-4o" in saved


@pytest.mark.anyio
async def test_anysearch_config_can_be_saved_without_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(web_app, "ENV_PATH", env_path)
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "duckduckgo")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/search",
            json={"provider": "anysearch"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["search_provider"] == "anysearch"
    assert payload["has_search_api_key"] is False
    assert payload["search_key_required"] is False
    assert "SEARCH_API_PROVIDER=anysearch" in env_path.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_search_config_rejects_keyed_provider_without_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_app, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "SEARCH_API_KEY", "")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/config/search", json={"provider": "serpapi"}
        )

    assert response.status_code == 400
    assert "SEARCH_API_KEY" in response.json()["detail"]


@pytest.mark.anyio
async def test_search_config_rejects_unknown_provider(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/api/config/search", json={"provider": "bing"})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_search_test_endpoint_reports_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试搜索端点把 provider 的报错透传为 400，而不是假装成功。"""
    async def failing(query: str, num_results: int = 5) -> str:
        return "Error: tavily search failed: invalid key"

    monkeypatch.setattr(web_app, "web_search", failing)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/config/search/test")

    assert response.status_code == 400
    assert "invalid key" in response.json()["detail"]


@pytest.mark.anyio
async def test_search_test_endpoint_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def working(query: str, num_results: int = 5) -> str:
        return "1. 结果标题\n   https://example.com\n   摘要"

    monkeypatch.setattr(web_app, "web_search", working)
    monkeypatch.setattr(config, "SEARCH_API_PROVIDER", "duckduckgo")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/config/search/test")

    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "duckduckgo"
    assert "结果标题" in payload["preview"]


@pytest.mark.anyio
async def test_config_exposes_supported_search_providers() -> None:
    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = (await client.get("/api/config")).json()

    assert payload["search_providers"] == [
        "anysearch",
        "duckduckgo",
        "serpapi",
        "tavily",
    ]


@pytest.mark.anyio
async def test_web_strategist_asks_before_drafting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent1 调用 AskUser 时返回澄清问题而非提纲（backlog 第 11 项）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="需求澄清", date_str="20260728")
    state.save()

    captured: dict[str, object] = {}

    async def fake_run_agent(prompt, options, client, registry, **kwargs):
        captured["tools"] = options.allowed_tools
        return await registry.execute(
            "AskUser", {"questions": ["调研地域？建议国内", "时间窗口？建议 3 年"]}
        )

    monkeypatch.setattr(web_app, "run_agent", fake_run_agent)
    monkeypatch.setattr(web_app.LLMClient, "__aenter__", lambda self: _async_none(self))
    monkeypatch.setattr(web_app.LLMClient, "__aexit__", _async_exit)

    outcome = await web_app._run_web_strategist(state, None)

    assert outcome.needs_clarification is True
    assert outcome.questions == ("调研地域？建议国内", "时间窗口？建议 3 年")
    assert "AskUser" in captured["tools"]


async def _async_none(value):
    return value


async def _async_exit(*args, **kwargs):
    return None


@pytest.mark.anyio
async def test_web_strategist_prefers_outline_over_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若 Agent1 既提问又写了提纲，以提纲为准（说明它已能收敛）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="同时产出", date_str="20260728")
    state.save()
    outline = state.project_dir / config.FILE_OUTLINE

    async def fake_run_agent(prompt, options, client, registry, **kwargs):
        await registry.execute("AskUser", {"questions": ["还要问一句"]})
        outline.write_text("# outline", encoding="utf-8")
        return ""

    monkeypatch.setattr(web_app, "run_agent", fake_run_agent)
    monkeypatch.setattr(web_app.LLMClient, "__aenter__", lambda self: _async_none(self))
    monkeypatch.setattr(web_app.LLMClient, "__aexit__", _async_exit)

    outcome = await web_app._run_web_strategist(state, None)

    assert outcome.needs_clarification is False
    assert outcome.outline_path == outline


@pytest.mark.anyio
async def test_web_strategist_drops_ask_tool_after_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """澄清条数用尽后不再提供 AskUser，强制收敛。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="用尽预算", date_str="20260728")
    state.clarification = [
        {"question": f"Q{index}", "answer": "A"}
        for index in range(web_app.STRATEGIST_MAX_CLARIFY_ROUNDS * 3)
    ]
    state.save()
    outline = state.project_dir / config.FILE_OUTLINE
    captured: dict[str, object] = {}

    async def fake_run_agent(prompt, options, client, registry, **kwargs):
        captured["tools"] = options.allowed_tools
        outline.write_text("# outline", encoding="utf-8")
        return ""

    monkeypatch.setattr(web_app, "run_agent", fake_run_agent)
    monkeypatch.setattr(web_app.LLMClient, "__aenter__", lambda self: _async_none(self))
    monkeypatch.setattr(web_app.LLMClient, "__aexit__", _async_exit)

    outcome = await web_app._run_web_strategist(state, None)

    assert "AskUser" not in captured["tools"]
    assert outcome.outline_path == outline


@pytest.mark.anyio
async def test_clarification_endpoint_records_answers_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    web_app.LOCKS.clear()
    state = ProjectState(
        topic="提交回答", date_str="20260728", stage=Stage.AWAIT_CLARIFICATION
    )
    state.notes["clarification_questions"] = ["地域？", "受众？"]
    state.save()
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/clarification",
            json={"answers": ["全球", "投资决策层"]},
        )

    assert response.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.PLANNING
    assert [item["answer"] for item in reloaded.clarification] == ["全球", "投资决策层"]
    assert "clarification_questions" not in reloaded.notes


@pytest.mark.anyio
async def test_clarification_skip_uses_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(
        topic="跳过澄清", date_str="20260728", stage=Stage.AWAIT_CLARIFICATION
    )
    state.notes["clarification_questions"] = ["地域？"]
    state.save()
    monkeypatch.setattr(web_app, "_schedule", lambda pid: None)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/clarification",
            json={"skip": True},
        )

    assert response.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.stage == Stage.PLANNING
    assert "用户未回答" in reloaded.clarification[0]["answer"]


@pytest.mark.anyio
async def test_clarification_rejected_when_not_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(topic="非澄清阶段", date_str="20260728", stage=Stage.SOURCING)
    state.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/clarification",
            json={"answers": []},
        )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_clarification_exposed_in_project_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(
        topic="payload", date_str="20260728", stage=Stage.AWAIT_CLARIFICATION
    )
    state.notes["clarification_questions"] = ["地域？"]
    state.clarification = [{"question": "旧问题", "answer": "旧答案"}]
    state.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = (await client.get(f"/api/projects/{state.project_dir.name}")).json()

    assert payload["clarification_questions"] == ["地域？"]
    assert payload["clarification"][0]["question"] == "旧问题"


@pytest.mark.anyio
async def test_usage_endpoint_aggregates_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/usage 汇总全部项目用量（backlog 第 5 项）。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "LLM_MODEL", "deepseek-v4-flash")

    first = ProjectState(topic="甲项目", date_str="20260728")
    first.token_usage = {
        "prompt_tokens": 8000,
        "completion_tokens": 2000,
        "total_tokens": 10000,
        "calls": 12,
        "stages": {"Agent2·采集": {"total_tokens": 7000, "calls": 8}},
    }
    first.save()
    second = ProjectState(topic="乙项目", date_str="20260728")
    second.token_usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
        "calls": 3,
        "stages": {"Agent4·深度分析": {"total_tokens": 1500, "calls": 3}},
    }
    second.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = (await client.get("/api/usage")).json()

    assert payload["total_tokens"] == 11500
    assert payload["calls"] == 15
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["peak_project_topic"] == "甲项目"
    stages = {item["stage"]: item["total_tokens"] for item in payload["stages"]}
    assert stages == {"Agent2·采集": 7000, "Agent4·深度分析": 1500}
    assert [item["topic"] for item in payload["projects"]] == ["甲项目", "乙项目"]


@pytest.mark.anyio
async def test_usage_endpoint_handles_empty_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有任何项目时返回零值而非报错。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "empty")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = (await client.get("/api/usage")).json()

    assert payload["total_tokens"] == 0
    assert payload["daily"] == []
    assert payload["stages"] == []
    assert payload["current_streak"] == 0


@pytest.mark.anyio
async def test_usage_endpoint_clamps_day_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_small = (await client.get("/api/usage?days=1")).json()
        too_large = (await client.get("/api/usage?days=9999")).json()

    assert too_small["days"] == 7
    assert too_large["days"] == 364


@pytest.mark.anyio
async def test_project_payload_exposes_token_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(topic="单项目用量", date_str="20260728")
    state.token_usage = {"total_tokens": 4321, "calls": 5}
    state.save()

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = (await client.get(f"/api/projects/{state.project_dir.name}")).json()

    assert payload["token_usage"]["total_tokens"] == 4321


@pytest.mark.anyio
async def test_manual_typeset_clears_stale_layout_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动重排版成功后应清除排版失败标记，否则项目一直显示"运行失败"。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(topic="重排版清标记", date_str="20260728", stage=Stage.FORMATTING)
    report = state.project_dir / config.FILE_FINAL_REPORT
    state.final_report_path = str(report)
    state.notes["latex_typeset_error"] = "PDF 存在严重排版警告"
    state.save()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# 报告", encoding="utf-8")
    state.mark_failure("Agent5·排版交付", "Agent5 排版交付物生成失败：PDF 存在严重排版警告")

    async def fake_artifacts(**kwargs):
        return {
            "tex_path": state.project_dir / config.FILE_FINAL_REPORT_TEX,
            "manifest_path": state.project_dir / config.FILE_CHART_MANIFEST,
            "html_path": state.project_dir / config.FILE_FINAL_REPORT_HTML,
            "pdf_path": state.project_dir / config.FILE_FINAL_REPORT_PDF,
            "engine": "/usr/bin/xelatex",
        }

    monkeypatch.setattr(web_app, "generate_typeset_artifacts", fake_artifacts)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/projects/{state.project_dir.name}/typeset/final-report"
        )

    assert response.status_code == 200
    reloaded = ProjectState.load(state.project_dir)
    assert reloaded.failed_stage is None
    assert reloaded.last_error is None
    assert "latex_typeset_error" not in reloaded.notes


@pytest.mark.anyio
async def test_manual_typeset_keeps_unrelated_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非排版阶段的失败不应被重排版顺手清掉。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    web_app.JOBS.clear()
    state = ProjectState(topic="保留他因", date_str="20260728", stage=Stage.FORMATTING)
    report = state.project_dir / config.FILE_FINAL_REPORT
    state.final_report_path = str(report)
    state.save()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# 报告", encoding="utf-8")
    state.mark_failure("Agent4·深度分析", "LLM timeout")

    async def fake_artifacts(**kwargs):
        return {
            "tex_path": state.project_dir / config.FILE_FINAL_REPORT_TEX,
            "manifest_path": state.project_dir / config.FILE_CHART_MANIFEST,
            "html_path": state.project_dir / config.FILE_FINAL_REPORT_HTML,
            "pdf_path": state.project_dir / config.FILE_FINAL_REPORT_PDF,
            "engine": "/usr/bin/xelatex",
        }

    monkeypatch.setattr(web_app, "generate_typeset_artifacts", fake_artifacts)

    transport = httpx.ASGITransport(app=web_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(f"/api/projects/{state.project_dir.name}/typeset/final-report")

    assert ProjectState.load(state.project_dir).failed_stage == "Agent4·深度分析"
