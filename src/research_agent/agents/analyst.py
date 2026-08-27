"""Agent4 · 深度分析，严格受已验证 EvidenceRecord 边界约束。"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from rich.console import Console

from .. import config
from ..agent_loop import AgentOptions, run_agent
from ..agent_skills import load_project_skill
from ..llm import LLMClient
from ..research_plan import load_plan, plan_prompt_context
from ..tools import default_registry
from .source_context import analyst_evidence_context

if TYPE_CHECKING:
    from ..state import ProjectState

console = Console()

_PROMPT_ANALYST = Path(__file__).parent / "prompts" / "analyst.md"
ANALYST_ALLOWED_TOOLS = (
    "Read",
    "Write",
    "ListProjectSources",
    "InspectSourceEvidence",
)
CLAIMS_REPAIR_ALLOWED_TOOLS = ("Read", "Write")


class AnalysisOutcomeError(RuntimeError):
    """AnalysisOutcome is missing, invalid, or inconsistent with the fixed plan."""


class AnalysisStatus(str, Enum):
    COMPLETED = "completed"
    NEEDS_MORE_RESEARCH = "needs_more_research"


class GapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    needed_evidence: str = Field(min_length=1)

    @field_validator("question_id", "reason", "needed_evidence")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    status: AnalysisStatus
    gap_requests: list[GapRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_gaps(self) -> "AnalysisOutcome":
        if self.status == AnalysisStatus.COMPLETED and self.gap_requests:
            raise ValueError("completed requires an empty gap_requests list")
        if self.status == AnalysisStatus.NEEDS_MORE_RESEARCH and not self.gap_requests:
            raise ValueError("needs_more_research requires at least one gap_request")
        return self


def load_analysis_outcome(
    state: "ProjectState",
    path: Path | None = None,
) -> AnalysisOutcome:
    """Load and validate Agent4's fail-closed transition contract."""
    outcome_path = path or state.project_dir / config.FILE_ANALYSIS_OUTCOME
    if not outcome_path.exists():
        raise AnalysisOutcomeError(f"AnalysisOutcome 文件缺失：{outcome_path}")
    raw = outcome_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise AnalysisOutcomeError(f"AnalysisOutcome 文件为空：{outcome_path}")
    try:
        outcome = AnalysisOutcome.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise AnalysisOutcomeError(
            f"AnalysisOutcome 文件损坏或不符合契约：{exc}"
        ) from exc

    allowed_question_ids = {
        requirement.question_id for requirement in load_plan(state).requirements
    }
    unknown = sorted(
        {
            request.question_id
            for request in outcome.gap_requests
            if request.question_id not in allowed_question_ids
        }
    )
    if unknown:
        raise AnalysisOutcomeError(
            "AnalysisOutcome 包含未知 question_id：" + ", ".join(unknown)
        )
    return outcome


def _load_analyst_prompt() -> str:
    return _PROMPT_ANALYST.read_text(encoding="utf-8")


async def run_analysis(state: "ProjectState") -> Path:
    """运行深度分析，输出 04_analysis.md。"""
    if not state.outline_path:
        raise RuntimeError("需要 outline.md")
    if not state.sources_final_path:
        raise RuntimeError("需要 sources_final.md")

    outline_path = Path(state.outline_path)
    sources_final_path = Path(state.sources_final_path)
    validation_report_path = (
        Path(state.validation_report_path)
        if state.validation_report_path
        else None
    )
    analysis_path = state.project_dir / config.FILE_ANALYSIS
    outcome_path = state.project_dir / config.FILE_ANALYSIS_OUTCOME
    claims_path = state.project_dir / config.FILE_CLAIMS

    # A rerun must not pass the gate with stale outputs from an earlier Agent4 run.
    analysis_path.unlink(missing_ok=True)
    outcome_path.unlink(missing_ok=True)
    claims_path.unlink(missing_ok=True)

    # Agent4 现在是唯一撰写正文的 Agent（Agent5 只出图表清单，正文被逐字复制），
    # 因此报告结构、表格与中文行文规范必须在这里注入。
    skill = load_project_skill(config.REPORT_FORMATTING_SKILL, role="analyst")
    system_prompt = _load_analyst_prompt() + skill.prompt_context()
    system_prompt += plan_prompt_context(state)
    system_prompt += analyst_evidence_context(state)
    replacements = {
        "{outline_path}": str(outline_path),
        "{sources_final_path}": str(sources_final_path),
        "{validation_report_path}": str(validation_report_path or "（无）"),
        "{analysis_path}": str(analysis_path),
        "{analysis_outcome_path}": str(outcome_path),
        "{claims_path}": str(claims_path),
        "{N}": str(state.collect_round),
    }
    for k, v in replacements.items():
        system_prompt = system_prompt.replace(k, v)

    system_prompt += (
        f"\n\n## 当前项目参数\n"
        f"- 调研主题：**{state.topic}**\n"
        f"- 提纲：`{outline_path}`\n"
        f"- 最终源清单：`{sources_final_path}`\n"
        f"- 验证报告：`{validation_report_path}`\n"
        f"- 分析报告输出路径：`{analysis_path}`\n"
        f"- 分析结果输出路径：`{outcome_path}`\n"
        f"- 结论台账输出路径：`{claims_path}`\n"
        f"- 研究需求清单：`{state.project_dir / config.FILE_RESEARCH_REQUIREMENTS}`\n"
        f"- 已加载写作规范 Skill：`{skill.name}`（报告结构、表格与中文行文规范）\n"
        f"- 你的 Markdown 会被逐字复制为最终交付报告，Agent5 只在其中插入图表占位符，"
        f"不会重写、补写或追加任何内容。\n"
    )

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=list(ANALYST_ALLOWED_TOOLS),
        cwd=str(state.project_dir),
        max_turns=50,
    )

    console.print(
        f"\n[bold magenta]═══ Agent4 · 深度分析 ═══[/bold magenta]\n"
        f"[dim]主题：{state.topic}[/dim]\n"
        f"[dim]数据轮次：{state.collect_round} 轮[/dim]\n"
    )

    async def _on_text(text: str) -> None:
        console.print(text, style="bright_blue", end="")

    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=(
                f"请仅基于 system prompt 中的 SUPPORTED EvidenceRecord 目录，"
                f"对「{state.topic}」做深度分析。读取提纲、源清单和验证报告时，"
                f"不得把其中未经验证的内容作为事实。将报告写入 `{analysis_path}`，"
                f"将 AnalysisOutcome 写入 `{outcome_path}`，并将与报告正文逐字对应的"
                f"结论台账写入 `{claims_path}`。"
            ),
            options=options,
            llm_client=client,
            tool_registry=default_registry,
            on_assistant_text=_on_text,
        )
    console.print()

    if not analysis_path.exists():
        raise RuntimeError(f"Agent4 未能生成分析报告：{analysis_path}")
    if not claims_path.exists():
        raise RuntimeError(f"Agent4 未能生成结论台账：{claims_path}")
    console.print(f"\n[green]✓ 深度分析完成：{analysis_path.name}[/green]")
    return analysis_path


async def repair_claims(
    state: "ProjectState",
    analysis_path: Path,
    audit_errors: list[str],
) -> Path:
    """只修正 Agent4 的机读结论台账，不改动已经生成的分析正文。"""
    claims_path = state.project_dir / config.FILE_CLAIMS
    if not analysis_path.is_file():
        raise RuntimeError(f"无法修正结论台账：分析报告不存在：{analysis_path}")

    error_summary = "\n".join(f"- {item}" for item in audit_errors)
    system_prompt = f"""你是 Agent4 的结论台账校正器。分析报告已经完成且不可修改；
你只能读取文件，并且只能重写 `{claims_path}`。不得改写 `{analysis_path}`、
`{state.project_dir / config.FILE_ANALYSIS_OUTCOME}` 或任何其他文件。

请读取 `{analysis_path}` 和现有的 `{claims_path}`，重新生成严格 JSON：
{{"schema_version":"1.0","claims":[...]}}。

硬性规则：
1. 每条 claim.text 必须对应分析报告中的一个连续结论片段：可以去掉正文里的 Markdown
   强调、来源引用和置信度标注，也可以微调连接词与语序，但关键数字必须与正文一致，
   不得摘要、拼接或新增数字；核心语义必须来自正文，不得新增结论。
2. 每个固定 question_id 至少一条 claim；claim_id 必须唯一。
3. supporting_evidence_ids 只能使用下方 SUPPORTED 目录中的 ID；尽量选择与
   claim.question_id 同 question 的证据，若没有完全同 question 的证据，可以挂
   语义最接近的同证据（evidence 必须真实且 SUPPORTED），不要伪造证据。
4. critical claim 至少挂一条 SUPPORTED 证据；无法满足时应选择正文中
   另一条能够满足的结论，不得伪造证据。
5. kind 只能是 fact/derivation/judgment，importance 只能是 critical/major/minor，
   confidence 只能是 high/medium/low。
6. 只保留覆盖固定研究问题和核心判断所需的最小 claim 集合；不要机械保留原有 26 条。

上一次确定性审计错误：
{error_summary}
"""
    system_prompt += plan_prompt_context(state)
    system_prompt += analyst_evidence_context(state)

    options = AgentOptions(
        system_prompt=system_prompt,
        model=config.LLM_MODEL,
        allowed_tools=list(CLAIMS_REPAIR_ALLOWED_TOOLS),
        cwd=str(state.project_dir),
        max_turns=15,
        stream=False,
    )
    async with LLMClient(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    ) as client:
        await run_agent(
            user_prompt=(
                f"修正 `{claims_path}`。先读取完整分析报告，再写入最小、逐字对应且"
                "问题—证据归属一致的结论台账。不要修改分析报告。"
            ),
            options=options,
            llm_client=client,
            tool_registry=default_registry,
        )

    if not claims_path.is_file():
        raise RuntimeError(f"结论台账校正未生成文件：{claims_path}")
    return claims_path
