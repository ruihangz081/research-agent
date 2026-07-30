"""R1 · 固定研究需求清单驱动的确定性门禁回归测试。

这些测试锁定 R1 的核心不变量：门禁必须读取**研究开始阶段生成的完整需求集合**，
而不是从已有 EvidenceRecord 反推。因此"某个必答问题一条证据都没有"必须被检测到。
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from research_agent import config
from research_agent.agents.validator import ValidationFeedback
from research_agent.orchestrator import (
    PipelineError,
    _assert_delivery_ready,
    _deterministic_convergence,
)
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord
from research_agent.sources.runtime import get_service, reset_runtime
from research_agent.state import ProjectState, Stage

# 持久化文件名属于对外契约，测试里写死字面量，避免与实现同步漂移。
REQUIREMENTS_FILE = "research_requirements.json"


def requirement(
    question_id: str,
    text: str = "研究问题",
    *,
    required: bool = True,
    min_supported: int = 1,
    min_source_tier: str | None = None,
    require_numeric: bool = False,
) -> dict:
    return {
        "question_id": question_id,
        "text": text,
        "required": required,
        "min_supported": min_supported,
        "min_source_tier": min_source_tier,
        "require_numeric": require_numeric,
    }


class Harness:
    """真实 SourceService + 真实 state 的最小项目夹具。"""

    def __init__(self, state: ProjectState) -> None:
        self.state = state
        self._counter = 0

    @property
    def project_id(self) -> str:
        return self.state.project_dir.name

    @property
    def service(self):
        return get_service(config.SOURCE_DATA_DIR)

    @property
    def plan_file(self) -> Path:
        return self.state.project_dir / REQUIREMENTS_FILE

    def write_plan(self, requirements: list[dict], **overrides) -> Path:
        question_lines = [
            f"{index}. {item.get('text') or '研究问题'}"
            for index, item in enumerate(requirements, 1)
        ] or ["1. 占位研究问题"]
        outline_text = (
            f"# 《{self.state.topic}》调研提纲\n\n"
            "## 二、核心研究问题\n"
            + "\n".join(question_lines)
            + "\n"
        )
        outline = self.state.project_dir / config.FILE_OUTLINE
        outline.write_text(outline_text, encoding="utf-8")
        self.state.outline_path = str(outline)
        self.state.save()
        payload = {
            "schema_version": 1,
            "topic": self.state.topic,
            "source_outline": hashlib.sha256(
                outline_text.encode("utf-8")
            ).hexdigest()[:16],
            "requirements": requirements,
        }
        payload.update(overrides)
        self.plan_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.plan_file

    def write_raw_plan(self, text: str) -> Path:
        self.plan_file.write_text(text, encoding="utf-8")
        return self.plan_file

    def write_outline(self, body: str) -> Path:
        path = self.state.project_dir / config.FILE_OUTLINE
        path.write_text(body, encoding="utf-8")
        self.state.outline_path = str(path)
        self.state.save()
        return path

    def add_evidence(
        self,
        question_id: str,
        *,
        tier: str = "S",
        numeric: str | float | None = None,
        status: VerificationStatus = VerificationStatus.SUPPORTED,
    ) -> EvidenceRecord:
        self._counter += 1
        claim = f"Metric {self._counter} for {question_id} is 42 million"
        service = self.service
        source = service.register_bytes(
            self.project_id, f"fact-{self._counter}.txt", claim.encode("utf-8")
        ).source
        source.source_tier = tier
        service.repository.update_source(source)
        service.parse_source(self.project_id, source.source_id)
        chunks = service.index_source(self.project_id, source.source_id)
        service.activate(self.project_id, source.source_id)
        evidence = EvidenceRecord(
            evidence_id=f"ev_{self._counter}",
            project_id=self.project_id,
            research_question_id=question_id,
            claim=claim,
            normalized_value=numeric,
            source_id=source.source_id,
            source_version=source.version,
            chunk_id=chunks[0].chunk_id,
            locator=chunks[0].locators[0],
            excerpt=claim,
            source_tier=tier,
            verification_status=status,
            confidence=1,
        )
        service.record_evidence(evidence)
        return evidence

    def drop_evidence(self, question_id: str) -> int:
        service = self.service
        removed = 0
        for item in service.repository.list_evidence(self.project_id):
            if item.research_question_id == question_id:
                service.repository.delete_evidence(item.evidence_id, self.project_id)
                removed += 1
        return removed


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    reset_runtime()
    state = ProjectState(topic="需求清单", date_str="20260730", stage=Stage.FORMATTING)
    state.project_dir.mkdir(parents=True)
    state.save()
    yield Harness(state)
    reset_runtime()


# ═══════════════════════════════════════════════════════════════
# 1—4：固定需求集合的覆盖判定
# ═══════════════════════════════════════════════════════════════


def test_required_question_with_zero_evidence_blocks_delivery(harness: Harness) -> None:
    """必答问题一条证据都没有：即使别的问题证据充足，也必须阻断。"""
    harness.write_plan([requirement("q1", "市场规模多大？"), requirement("q2", "竞争格局如何？")])
    harness.add_evidence("q1")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    reasons = " ".join(harness.state.notes["quality_gate_reasons"])
    assert "q2" in reasons
    assert harness.state.notes["quality_gate"] in {"needs_more_research", "blocked"}


def test_partial_required_coverage_blocks_delivery(harness: Harness) -> None:
    """多个必答问题只覆盖了一部分：阻断。"""
    harness.write_plan(
        [requirement("q1"), requirement("q2"), requirement("q3")]
    )
    harness.add_evidence("q1")
    harness.add_evidence("q2")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    assert "q3" in " ".join(harness.state.notes["quality_gate_reasons"])


def test_deleting_all_evidence_of_one_question_blocks_delivery(harness: Harness) -> None:
    """全部必答问题达标 → 允许继续；删掉某个问题的全部证据 → 重新阻断。"""
    harness.write_plan([requirement("q1"), requirement("q2")])
    harness.add_evidence("q1")
    harness.add_evidence("q2")

    _assert_delivery_ready(harness.state)  # 达标，不抛异常

    assert harness.drop_evidence("q2") == 1

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    assert "q2" in " ".join(harness.state.notes["quality_gate_reasons"])


def test_optional_question_without_evidence_does_not_block(harness: Harness) -> None:
    """可选问题没有证据：不单独导致失败。"""
    harness.write_plan(
        [requirement("q1"), requirement("q2", required=False)]
    )
    harness.add_evidence("q1")

    _assert_delivery_ready(harness.state)

    assert harness.state.notes["quality_gate"] in {"passed", "passed_with_limitations"}


# ═══════════════════════════════════════════════════════════════
# 5—8：需求清单缺失 / 为空 / 重复 ID / 格式损坏
# ═══════════════════════════════════════════════════════════════


def test_missing_requirements_file_blocks_and_asks_for_migration(harness: Harness) -> None:
    """旧项目没有需求文件：不得按空集合放行，必须提示迁移动作。"""
    harness.add_evidence("legacy:1")

    with pytest.raises(PipelineError) as exc_info:
        _assert_delivery_ready(harness.state)

    message = str(exc_info.value)
    assert REQUIREMENTS_FILE in message
    assert "migrate-plan" in message
    assert harness.state.notes["research_plan_migration_required"] is True


def test_empty_requirements_file_blocks_delivery(harness: Harness) -> None:
    harness.write_plan([])
    harness.add_evidence("q1")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    assert harness.state.notes["quality_gate"] == "blocked"


def test_duplicate_question_ids_block_delivery(harness: Harness) -> None:
    harness.write_plan([requirement("q1"), requirement("q1", "重复 ID")])
    harness.add_evidence("q1")

    with pytest.raises(PipelineError) as exc_info:
        _assert_delivery_ready(harness.state)

    assert "q1" in str(exc_info.value)


def test_blank_question_id_blocks_delivery(harness: Harness) -> None:
    harness.write_plan([requirement("  ", "空 ID")])
    harness.add_evidence("q1")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)


def test_corrupted_requirements_file_blocks_delivery(harness: Harness) -> None:
    harness.write_raw_plan("{not json at all")
    harness.add_evidence("q1")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    assert harness.state.notes["research_plan_migration_required"] is True


def test_unknown_schema_version_blocks_delivery(harness: Harness) -> None:
    harness.write_plan([requirement("q1")], schema_version=999)
    harness.add_evidence("q1")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)


def test_outline_change_invalidates_fixed_requirements(harness: Harness) -> None:
    """提纲生成后发生变化时，旧清单不得继续代表当前研究范围。"""
    harness.write_plan([requirement("q1", "原研究问题？")])
    harness.add_evidence("q1")
    _assert_delivery_ready(harness.state)

    outline = harness.state.project_dir / config.FILE_OUTLINE
    outline.write_text(
        "# 《需求清单》调研提纲\n\n## 二、核心研究问题\n1. 已修改的研究问题？\n",
        encoding="utf-8",
    )

    with pytest.raises(PipelineError, match="发生变化"):
        _assert_delivery_ready(harness.state)

    assert harness.state.notes["research_plan_migration_required"] is True


# ═══════════════════════════════════════════════════════════════
# 9—11：数值门槛 / 来源等级 / 未知 question_id
# ═══════════════════════════════════════════════════════════════


def test_numeric_requirement_without_numeric_evidence_blocks(harness: Harness) -> None:
    harness.write_plan([requirement("q1", require_numeric=True)])
    harness.add_evidence("q1", numeric=None)

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    harness.drop_evidence("q1")
    harness.add_evidence("q1", numeric=42)
    _assert_delivery_ready(harness.state)


def test_source_tier_below_threshold_blocks(harness: Harness) -> None:
    harness.write_plan([requirement("q1", min_source_tier="S")])
    harness.add_evidence("q1", tier="A")

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    harness.add_evidence("q1", tier="S")
    _assert_delivery_ready(harness.state)


def test_unknown_question_id_is_not_silently_ignored(harness: Harness) -> None:
    """证据引用了需求清单之外的问题 ID：必须显式报告，不能当作正常通过。"""
    harness.write_plan([requirement("q1")])
    harness.add_evidence("q1")
    harness.add_evidence("q_not_in_plan")

    _assert_delivery_ready(harness.state)  # q1 已达标，交付不因此硬阻断

    reasons = " ".join(harness.state.notes["quality_gate_reasons"])
    assert "q_not_in_plan" in reasons
    assert harness.state.notes["quality_gate"] == "passed_with_limitations"


@pytest.mark.anyio
async def test_record_evidence_tool_rejects_unknown_question_id(harness: Harness) -> None:
    from research_agent.tools.builtins import project_sources

    harness.write_plan([requirement("q1")])
    evidence = harness.add_evidence("q1")
    source = harness.service.get_source(harness.project_id, evidence.source_id)
    chunk = harness.service.repository.get_chunk(evidence.chunk_id, harness.project_id)

    payload = json.loads(
        await project_sources.record_project_evidence(
            harness.project_id,
            "q_unknown",
            evidence.claim,
            source.source_id,
            source.version,
            chunk.chunk_id,
            evidence.excerpt,
            chunk.locators[0].model_dump_json(),
        )
    )

    assert payload["ok"] is False
    assert payload["error"] == "unknown_research_question_id"
    assert "q1" in payload["known_question_ids"]


# ═══════════════════════════════════════════════════════════════
# 12：采集验证收敛判定同样使用固定需求集合
# ═══════════════════════════════════════════════════════════════


def test_convergence_requires_full_plan_coverage(harness: Harness) -> None:
    """模型声明收敛 + 已有证据全部合格，但必答问题未覆盖全：不得判定收敛。"""
    harness.write_plan([requirement("q1"), requirement("q2")])
    harness.add_evidence("q1")
    feedback = ValidationFeedback(round=1, converged=True)

    assert _deterministic_convergence(harness.state, feedback) is False
    assert "q2" in " ".join(harness.state.notes["quality_gate_reasons"])

    harness.add_evidence("q2")
    assert _deterministic_convergence(harness.state, feedback) is True


def test_convergence_blocks_legacy_project_without_plan(harness: Harness) -> None:
    harness.add_evidence("q1")
    feedback = ValidationFeedback(round=1, converged=True)

    assert _deterministic_convergence(harness.state, feedback) is False
    assert harness.state.notes["research_plan_migration_required"] is True


# ═══════════════════════════════════════════════════════════════
# 13—16：Agent1 阶段生成清单、提纲重生成后清单同步
# ═══════════════════════════════════════════════════════════════

_OUTLINE_TEMPLATE = """# 《{topic}》调研提纲

## 一、调研元信息
- 调研目标：投资决策

## 二、核心研究问题（Key Questions）
{questions}

## 三、调研章节规划
### 1. 市场概况
"""


def outline_with(topic: str, questions: list[str]) -> str:
    body = "\n".join(f"{index}. {text}" for index, text in enumerate(questions, 1))
    return _OUTLINE_TEMPLATE.format(topic=topic, questions=body)


class _PlanHost:
    """最小状态机宿主：Agent1 写出提纲，检查点一律挂起。"""

    def __init__(self, outlines: list[str]) -> None:
        self.outlines = list(outlines)
        self.logs: list[str] = []

    async def run_strategist(self, state: ProjectState, feedback):
        path = state.project_dir / config.FILE_OUTLINE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.outlines.pop(0), encoding="utf-8")
        return path

    async def resolve_clarification(self, state: ProjectState, questions):
        return []

    async def resolve_checkpoint(self, state: ProjectState, spec):
        from research_agent import orchestrator

        return orchestrator.CheckpointResult(
            decision=orchestrator.CheckpointDecision.PAUSE
        )

    def log(self, message: str) -> None:
        self.logs.append(message)

    def announce_done(self, state: ProjectState) -> None:  # pragma: no cover
        return None


@pytest.mark.anyio
async def test_agent1_stage_persists_requirements_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需求清单在 Agent1 阶段（研究开始前）就被固化并落盘。"""
    from research_agent import orchestrator

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="清单生成", date_str="20260730")
    host = _PlanHost([outline_with("清单生成", ["市场规模有多大？", "竞争格局如何？", "政策风险几何？"])])

    await orchestrator.run_state_machine(state, host)

    assert state.stage == Stage.AWAIT_OUTLINE_APPROVAL
    plan_file = state.project_dir / REQUIREMENTS_FILE
    assert plan_file.is_file()
    payload = json.loads(plan_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert [item["question_id"] for item in payload["requirements"]] == ["q1", "q2", "q3"]
    assert payload["requirements"][0]["text"] == "市场规模有多大？"
    assert all(item["required"] for item in payload["requirements"])
    assert state.research_plan_path == str(plan_file)
    assert any("研究需求清单" in message for message in host.logs)


@pytest.mark.anyio
async def test_regenerated_outline_keeps_plan_in_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent1 重新生成提纲后，需求清单必须随之重建，不残留旧问题。"""
    from research_agent import orchestrator

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="清单同步", date_str="20260730")
    first = outline_with("清单同步", ["旧问题 A？", "旧问题 B？"])
    second = outline_with("清单同步", ["新问题 X？", "新问题 Y？", "新问题 Z？"])
    host = _PlanHost([first, second])

    await orchestrator.run_state_machine(state, host)
    before = json.loads((state.project_dir / REQUIREMENTS_FILE).read_text(encoding="utf-8"))
    assert [item["text"] for item in before["requirements"]] == ["旧问题 A？", "旧问题 B？"]

    # 模拟用户驳回提纲：状态机回到 PLANNING，Agent1 产出第二版提纲
    state.notes["outline_feedback"] = "请换一批研究问题"
    state.advance_to(Stage.PLANNING)
    await orchestrator.run_state_machine(state, host)

    after = json.loads((state.project_dir / REQUIREMENTS_FILE).read_text(encoding="utf-8"))
    assert [item["text"] for item in after["requirements"]] == [
        "新问题 X？",
        "新问题 Y？",
        "新问题 Z？",
    ]
    assert after["source_outline"] != before["source_outline"]


def test_outline_without_question_section_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提纲缺《核心研究问题》时必须阻断，不能用章节标题伪造必答问题全集。"""
    from research_agent import research_plan
    from research_agent.research_plan import ResearchPlanError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="兜底", date_str="20260730")
    state.project_dir.mkdir(parents=True)
    outline = state.project_dir / config.FILE_OUTLINE
    outline.write_text(
        "# 《兜底》调研提纲\n\n## 三、调研章节规划\n### 1. 市场概况\n### 2. 竞争格局\n",
        encoding="utf-8",
    )
    state.outline_path = str(outline)
    state.save()

    with pytest.raises(ResearchPlanError, match="核心研究问题"):
        research_plan.rebuild_plan(state)

    assert not (state.project_dir / REQUIREMENTS_FILE).exists()


def test_rebuild_requires_an_outline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from research_agent import research_plan
    from research_agent.research_plan import ResearchPlanError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="无提纲", date_str="20260730")
    state.project_dir.mkdir(parents=True)
    state.save()

    with pytest.raises(ResearchPlanError, match="还没有调研提纲"):
        research_plan.rebuild_plan(state)


def test_cli_migrate_plan_command_rebuilds_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """CLI 与 Web 走同一个迁移函数；命令行必须给出可执行的下一步。"""
    from research_agent.__main__ import main

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="CLI迁移", date_str="20260730", stage=Stage.FORMATTING)
    state.project_dir.mkdir(parents=True)
    outline = state.project_dir / config.FILE_OUTLINE
    outline.write_text(outline_with("CLI迁移", ["问题一？", "问题二？"]), encoding="utf-8")
    state.outline_path = str(outline)
    state.save()

    assert main(["migrate-plan", str(state.project_dir)]) == 0
    assert (state.project_dir / REQUIREMENTS_FILE).is_file()

    # 没有提纲的项目不能被静默迁移成空清单
    empty = ProjectState(topic="CLI无提纲", date_str="20260730")
    empty.project_dir.mkdir(parents=True)
    empty.save()
    assert main(["migrate-plan", str(empty.project_dir)]) == 2
    assert not (empty.project_dir / REQUIREMENTS_FILE).exists()


def test_migration_does_not_overwrite_valid_plan(harness: Harness) -> None:
    """迁移只修复旧项目；有效清单及人工收紧的门槛必须原样保留。"""
    from research_agent.orchestrator import migrate_research_plan
    from research_agent.research_plan import ResearchPlanError

    path = harness.write_plan(
        [
            requirement(
                "q1",
                "市场规模是多少？",
                min_supported=2,
                min_source_tier="S",
                require_numeric=True,
            )
        ]
    )
    before = path.read_bytes()

    with pytest.raises(ResearchPlanError, match="现有清单未被覆盖"):
        migrate_research_plan(harness.state)

    assert path.read_bytes() == before


def test_cli_migrate_plan_rejects_valid_plan(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    from research_agent.__main__ import main

    path = harness.write_plan([requirement("q1", min_supported=2)])
    before = path.read_bytes()

    assert main(["migrate-plan", str(harness.state.project_dir)]) == 2
    assert path.read_bytes() == before
    output = "".join(capsys.readouterr().out.split())
    assert "现有清单未被覆盖" in output


# ═══════════════════════════════════════════════════════════════
# 17—19：工具层边界 + CLI/Web 使用同一份读取与门禁逻辑
# ═══════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_evidence_tool_rejects_project_id_escaping_projects_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_id 来自模型输出，不能靠 `..` 读到项目根目录之外的清单文件。"""
    from research_agent.tools.builtins import project_sources

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "SOURCE_DATA_DIR", tmp_path / "sources")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / REQUIREMENTS_FILE).write_text(
        json.dumps({"schema_version": 1, "requirements": [requirement("q1")]}),
        encoding="utf-8",
    )
    reset_runtime()

    payload = json.loads(await project_sources.record_project_evidence(
        "../outside",
        "q1",
        "claim",
        "src_x",
        1,
        "chunk_x",
        "excerpt",
        json.dumps({"locator_type": "offset"}),
    ))

    assert payload["ok"] is False
    assert payload["error"] == "missing_research_requirements"
    reset_runtime()


def test_cli_and_web_share_one_requirement_reader_and_gate() -> None:
    """CLI 与 Web 不允许各自实现一套读取或门禁逻辑。"""
    import inspect

    from research_agent import __main__ as cli
    from research_agent import orchestrator, research_plan, web_app

    # 唯一的需求读取入口
    assert web_app.load_plan_or_none is research_plan.load_plan_or_none
    assert cli.load_plan_or_none is research_plan.load_plan_or_none
    # 唯一的门禁入口与唯一的迁移入口
    assert web_app._assert_delivery_ready is orchestrator._assert_delivery_ready
    assert web_app.migrate_research_plan is orchestrator.migrate_research_plan
    # 门禁只从固定清单取要求，不从证据反推
    source = inspect.getsource(orchestrator._assert_delivery_ready)
    assert "_fixed_requirements" in source
    assert "research_question_id" not in source
    assert "research_question_id" not in inspect.getsource(
        orchestrator._deterministic_convergence
    )
    assert "research_question_id" not in inspect.getsource(
        __import__("research_agent.agents.formatter", fromlist=["x"])._require_delivery_evidence
    )


def test_quality_gate_reasons_name_the_question_text(harness: Harness) -> None:
    """门禁原因里带上问题文本，用户不用去翻 JSON 才知道缺了什么。"""
    harness.write_plan([requirement("q1", "2024 年市场规模是多少？")])

    with pytest.raises(PipelineError):
        _assert_delivery_ready(harness.state)

    reasons = " ".join(harness.state.notes["quality_gate_reasons"])
    assert "2024 年市场规模是多少？" in reasons


def test_migration_uses_outline_inside_the_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """state.json 里的绝对路径可能指向被复制前的旧目录，迁移必须用当前项目内的提纲。"""
    from research_agent import research_plan

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    stale = tmp_path / "old-location" / "01_outline.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "# 旧\n\n## 二、核心研究问题\n1. 旧目录里的问题？\n", encoding="utf-8"
    )

    state = ProjectState(topic="复制项目", date_str="20260730")
    state.project_dir.mkdir(parents=True)
    (state.project_dir / config.FILE_OUTLINE).write_text(
        "# 新\n\n## 二、核心研究问题\n1. 当前目录里的问题？\n", encoding="utf-8"
    )
    state.outline_path = str(stale)
    state.save()

    plan, _warning = research_plan.rebuild_plan(state)

    assert [item.text for item in plan.requirements] == ["当前目录里的问题？"]
    assert state.outline_path == str(state.project_dir / config.FILE_OUTLINE)
