"""P0/P1/P2/P3 回归测试：引用归一化、字形覆盖、ev 引用展开、确定性错误。

每个测试都要能复现原 bug——即在未改代码前会失败：
- 融合式 ``[事实｜src:...]`` 之前不被 ``extract_standard_citations`` 计入；
- 替换表外的 ⑪、㎡ 之前会静默丢进 PDF 而不到阶段级失败；
- 截断的 ``ev_`` id 之前会被 tasks 的保守前缀解析静默自愈（而不是报错）；
- 只含 ``[ev=...]`` 的正文之前无法通过 ``audit_analysis_citations``。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_agent import config
from research_agent.orchestrator import _safe_run
from research_agent.pipeline_errors import DeterministicContentError, PipelineError
from research_agent.report_formatting import (
    canonicalize_report_text,
    check_pdf_glyph_coverage,
    count_merged_citations,
    normalize_markdown_for_pdf,
    normalize_merged_citations,
)
from research_agent.sources import LocalObjectStore, SQLiteRepository, SourceService
from research_agent.sources.citations import (
    audit_analysis_citations,
    expand_evidence_citations,
    extract_standard_citations,
    render_citation,
)
from research_agent.sources.claims import normalize_claim_text
from research_agent.sources.enums import VerificationStatus
from research_agent.sources.models import EvidenceRecord
from research_agent.state import ProjectState


# ═══════════════════════════════════════════════════════════════
# P0：融合式引用归一化收敛到单一入口
# ═══════════════════════════════════════════════════════════════


def test_merged_citation_is_normalized_and_counted() -> None:
    """融合式 ``[事实｜src:...]`` 被拆回 ``[事实] [src:...]``，且被引用提取计入。

    修改前：``extract_standard_citations`` 只认 ``[src:...]``，融合式 125 条全部
    漏计（当前项目 67 → 应为 192）。
    """
    merged = (
        "[事实｜src:src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:v1, "
        "ev=ev_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, chunk=chk_1, paragraph=3]"
    )
    text = f"结论甲 {merged} 结论乙 [src:src_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:v2, ev=ev_2]"

    assert count_merged_citations(text) == 1
    normalized = normalize_merged_citations(text)
    assert "[事实｜src:" not in normalized
    assert "[事实] [src:src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:v1" in normalized

    citations = extract_standard_citations(normalized)
    assert len(citations) == 2
    assert citations[0].source_id == "src_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_canonicalize_expands_ev_after_normalization(
    tmp_path: Path,
) -> None:
    """canonicalize 先拆融合引用、再展开 ``[ev=...]``，两个步骤可串联。"""
    repository, service, source, evidence = _setup_project(tmp_path)
    try:
        merged = (
            f"[事实｜src:{source.source_id}:v1, ev={evidence.evidence_id}, "
            f"chunk={evidence.chunk_id}, paragraph=1]"
        )
        text = f"结论甲 {merged} 结论乙 [ev={evidence.evidence_id}]"

        canonical = canonicalize_report_text(
            text, repository=repository, project_id="project"
        )
        # 融合引用已拆分，ev 已展开为标准引用
        assert "[事实｜src:" not in canonical
        assert "[事实] [src:" in canonical
        assert canonical.count("[src:") == 2
    finally:
        repository.close()


def test_claim_normalization_accepts_merged_citation_form() -> None:
    """claims 归一化必须能剥掉融合式引用，否则台账结论文本会被误判不在正文中。"""
    body = "营收 4200 万。[事实｜src:src_a:v1, ev=ev-1, chunk=chk_a, p.1]"
    claim = "营收 4200 万"

    assert normalize_claim_text(claim) in normalize_claim_text(body)


def test_merged_ev_form_is_normalized_and_counted(
    tmp_path: Path,
) -> None:
    """混合文本（1 标准 + 1 融合 [事实｜ev=...]）归一化后引用计数必须为 2。

    缺口一：_MERGED_CITATION_RE 之前只认 ``(?:src:)?src_``，覆盖不到 P2 新格式的
    融合写法 ``[事实｜ev=ev_xxx]``——它会被 ``_ANALYSIS_ANNOTATION`` 整段吞掉，
    引用凭空消失且审计不报错。修复后该形式被拆成 ``[事实] [ev=...]`` 再展开。
    """
    repository, _service, _source, evidence = _setup_project(tmp_path)
    try:
        standard = "[src:src_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:v2, ev=ev_2]"
        merged_ev = f"[事实｜ev={evidence.evidence_id}]"
        text = f"结论甲 {standard} 结论乙 {merged_ev}"

        canonical = canonicalize_report_text(
            text, repository=repository, project_id="project"
        )

        assert extract_standard_citations(canonical).__len__() == 2
    finally:
        repository.close()


def test_leftover_merged_citation_is_rejected() -> None:
    """归一化后仍残留 ｜ev= / ｜src: 时必须 fail-closed，不得静默丢引用。"""
    # [假设｜ev=...]：假设在拆分白名单内，会被正确拆分，不应触发残留检查。
    assert "｜ev=" not in canonicalize_report_text("[假设｜ev=ev_a]")
    # 未知标注种类 + 融合引用：拆分白名单匹配不到，残留必须报错。
    with pytest.raises(DeterministicContentError, match="残留融合式引用"):
        canonicalize_report_text("[其它｜ev=ev_a]")


# ═══════════════════════════════════════════════════════════════
# P1：字形覆盖白名单
# ═══════════════════════════════════════════════════════════════


def test_glyph_check_captures_chars_outside_replacement_table() -> None:
    """替换表外的 ⑪、㎡ 必须被确定性字形检查捕获（而非静默丢字）。"""
    with pytest.raises(DeterministicContentError) as exc_info:
        check_pdf_glyph_coverage("面积 12 ㎡，排名第 ⑪ 位")
    message = str(exc_info.value)
    assert "U+33A1" in message  # ㎡ SQUARE M SQUARED
    assert "U+246A" in message  # ⑪ CIRCLED NUMBER ELEVEN
    assert "SQUARE M" in message
    assert "内容错误，重试无效" in message


def test_glyph_check_passes_after_replacement_table() -> None:
    """命中替换表的 ①⑥⁴≈≥↔ 先被换掉，字形检查剩余 0 个。"""
    raw = "① ② ⑥ ⁴ ≈ ≥ ↔ 元 港股"
    replaced = normalize_markdown_for_pdf(raw)
    check_pdf_glyph_coverage(replaced)  # 不抛错


def test_glyph_check_skips_when_latin_font_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 TeX 环境（kpsewhich 失败）时跳过字形检查，不误判 ×−÷± 为越界。

    缺口二：_latin_cmap 之前返回空 frozenset，把所有非 CJK 非 ASCII 字符判为越界；
    但无引擎的机器根本不会编译 PDF，×−÷± 这些西文字体本就覆盖的字符不该误阻断。
    """
    import research_agent.report_formatting as rf

    monkeypatch.setattr(rf, "_kpsewhich", lambda _font_name: None)
    # 含 × − ÷ ± 等西文符号，若无 TeX 时这些不应被误判。
    check_pdf_glyph_coverage("增长 2× − 3 ÷ 4 ± 0.5")


def test_glyph_check_allows_cjk_and_latin_covered() -> None:
    """CJK 与西文 cmap 覆盖的字符不误报。"""
    check_pdf_glyph_coverage("中文测试 — 数据 ± × ÷ Σ ∞ · ½ 号")


# ═══════════════════════════════════════════════════════════════
# P2：Agent4 只写 evidence_id，引用文本由程序展开
# ═══════════════════════════════════════════════════════════════


def _setup_project(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "catalog.sqlite3")
    service = SourceService(repository, LocalObjectStore(tmp_path / "objects"))
    result = service.register_bytes("project", "report.txt", b"Revenue was 42 million")
    source = result.source
    source.source_tier = "S"
    repository.update_source(source)
    service.parse_source("project", source.source_id)
    service.index_source("project", source.source_id)
    chunk = repository.all_chunks("project")[0]
    evidence = EvidenceRecord(
        evidence_id="ev_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project_id="project",
        research_question_id="q1",
        claim="Revenue was 42 million",
        source_id=source.source_id,
        source_version=source.version,
        chunk_id=chunk.chunk_id,
        locator=chunk.locators[0],
        excerpt="Revenue was 42 million",
        source_tier="S",
        verification_status=VerificationStatus.SUPPORTED,
        confidence=1,
    )
    service.record_evidence(evidence)
    return repository, service, source, evidence


def test_ev_only_citation_expands_to_standard_citation(tmp_path: Path) -> None:
    repository, service, source, evidence = _setup_project(tmp_path)
    try:
        text = f"结论甲 [事实] [ev={evidence.evidence_id}]"

        expanded = expand_evidence_citations(text, repository, "project")

        expected = render_citation(evidence, source)
        assert expanded == f"结论甲 [事实] {expected}"
        assert extract_standard_citations(expanded) == extract_standard_citations(
            f"结论甲 {expected}"
        )
    finally:
        repository.close()


def test_truncated_ev_id_raises_instead_of_silent_resolution(
    tmp_path: Path,
) -> None:
    """截断的 ev id 必须报错，而不是静默前缀解析成某条证据。"""
    repository, _service, _source, evidence = _setup_project(tmp_path)
    try:
        truncated = evidence.evidence_id[:-2]
        with pytest.raises(DeterministicContentError, match="未知 evidence_id"):
            expand_evidence_citations(
                f"[ev={truncated}]", repository, "project"
            )
    finally:
        repository.close()


def test_ev_only_analysis_passes_citation_audit(tmp_path: Path) -> None:
    """只含 ``[ev=...]`` 的分析正文，展开后 audit_analysis_citations 零错误。"""
    repository, _service, _source, evidence = _setup_project(tmp_path)
    try:
        text = f"Revenue was 42 million [事实] [ev={evidence.evidence_id}]"
        canonical = canonicalize_report_text(
            text, repository=repository, project_id="project"
        )
        assert audit_analysis_citations(canonical, "project", repository) == []
    finally:
        repository.close()


def test_unknown_ev_id_raises_instead_of_silent_skip(tmp_path: Path) -> None:
    repository, _service, _source, _evidence = _setup_project(tmp_path)
    try:
        with pytest.raises(DeterministicContentError, match="未知 evidence_id"):
            expand_evidence_citations(
                "[ev=ev_ffffffffffffffffffffffffffffffff]", repository, "project"
            )
    finally:
        repository.close()


def test_web_display_expands_ev_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「深度分析」页展示时，``[ev=ev_xxx]`` 必须展开为标准引用，不泄露内部 ID。

    修复前 ``_read_display_artifact`` 只拆融合式引用、不展开短引用，用户会在
    深度分析页看到 ``[ev=ev_1718622192894f4c9a5fcdf383341d49]`` 这类内部 ID 串。
    """
    from research_agent import web_app

    repository, service, source, evidence = _setup_project(tmp_path)
    try:
        monkeypatch.setattr(web_app, "_source_service", service)
        # 让 state.project_dir.name == "project"，与 _setup_project 的证据库对齐
        monkeypatch.setattr(
            config, "project_dir_for", lambda topic, date_str: tmp_path / "project"
        )
        state = ProjectState(topic="project", date_str="20260825")
        analysis = tmp_path / "04_analysis.md"
        analysis.write_text(
            f"结论甲 [事实] [ev={evidence.evidence_id}]",
            encoding="utf-8",
        )

        display = web_app._read_display_artifact(
            "analysis", analysis, state=state
        )
        # 内部短引用已被展开为标准引用，且不再有 [ev= 残留
        assert "[ev=" not in display
        assert "[src:" in display
        assert evidence.evidence_id in display  # 展开后的标准引用仍携带 ev id
    finally:
        repository.close()


# ═══════════════════════════════════════════════════════════════
# P3：确定性内容错误不进阶段级重试
# ═══════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_deterministic_content_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeterministicContentError 走 except PipelineError，_safe_run 不重试。"""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    state = ProjectState(topic="blocked", date_str="20260720")
    attempts = 0

    async def deterministic() -> None:
        nonlocal attempts
        attempts += 1
        raise DeterministicContentError("内容错误，重试无效，需修正上游产物")

    with pytest.raises(DeterministicContentError, match="内容错误，重试无效"):
        await _safe_run("Agent5", state, deterministic)

    assert attempts == 1
    assert state.retry_count == 0  # 未进入用户重试计数


@pytest.mark.anyio
async def test_tasks_error_is_classified_as_deterministic_content_error() -> None:
    """TasksError 继承 DeterministicContentError，落入不重试分支。"""
    from research_agent.sources.tasks import TasksError

    assert issubclass(TasksError, DeterministicContentError)
    assert issubclass(TasksError, PipelineError)


def test_glyph_failure_is_deterministic_content_error() -> None:
    """字形覆盖检查抛 DeterministicContentError（P3：不进阶段级重试）。"""
    try:
        check_pdf_glyph_coverage("排名第 ⑪ 位")
    except DeterministicContentError as exc:
        assert isinstance(exc, PipelineError)
        assert "内容错误，重试无效" in str(exc)
    else:
        pytest.fail("expected DeterministicContentError")

