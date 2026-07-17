"""状态机冒烟测试：不依赖 LLM API，只验证状态持久化与转移逻辑。"""
import json
import tempfile
from pathlib import Path

import sys
import types

# 注入 dotenv stub（避免测试环境缺依赖）
dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda *a, **kw: None
sys.modules["dotenv"] = dotenv_stub

# 注入 httpx stub（避免导入 llm 包时报错）
httpx_stub = types.ModuleType("httpx")
for cls_name in ["AsyncClient", "Timeout", "Limits", "HTTPError", "HTTPStatusError", "RequestError"]:
    setattr(httpx_stub, cls_name, type(cls_name, (), {}))
sys.modules["httpx"] = httpx_stub

# 注入 bs4 stub
bs4_stub = types.ModuleType("bs4")
bs4_stub.BeautifulSoup = type("BeautifulSoup", (), {})
sys.modules["bs4"] = bs4_stub

sys.path.insert(0, "src")

from research_agent.state import ProjectState, Stage  # noqa: E402

# === Test 1: 创建、保存、加载 ===
with tempfile.TemporaryDirectory() as tmp:
    import research_agent.config as cfg
    cfg.PROJECTS_DIR = Path(tmp)

    s = ProjectState(topic="测试行业", date_str="20260423")
    assert s.stage == Stage.INIT
    s.save()

    s2 = ProjectState.load(s.project_dir)
    assert s2.topic == "测试行业"
    assert s2.stage == Stage.INIT
    print("Test 1 passed: 状态创建/保存/加载 OK")

# === Test 2: 状态机推进 ===
with tempfile.TemporaryDirectory() as tmp:
    import research_agent.config as cfg
    cfg.PROJECTS_DIR = Path(tmp)

    s = ProjectState(topic="行业A", date_str="20260423")
    s.advance_to(Stage.PLANNING)
    assert s.stage == Stage.PLANNING
    s.advance_to(Stage.AWAIT_OUTLINE_APPROVAL)
    assert s.stage.is_checkpoint
    s.advance_to(Stage.SOURCING)
    s.advance_to(Stage.AWAIT_SOURCE_APPROVAL)
    assert s.stage.is_checkpoint
    s.advance_to(Stage.COLLECTING_AND_VALIDATING)
    print("Test 2 passed: 状态机推进 & checkpoint 判定 OK")

# === Test 3: 驳回循环（模拟 feedback 存储） ===
with tempfile.TemporaryDirectory() as tmp:
    import research_agent.config as cfg
    cfg.PROJECTS_DIR = Path(tmp)

    s = ProjectState(topic="行业B", date_str="20260423")
    s.advance_to(Stage.AWAIT_SOURCE_APPROVAL)
    # 模拟用户驳回
    s.notes["sources_feedback"] = "需要补充政策层面的源"
    s.advance_to(Stage.SOURCING)
    # 下一轮读取
    s2 = ProjectState.load(s.project_dir)
    assert s2.notes["sources_feedback"] == "需要补充政策层面的源"
    # 消费反馈
    fb = s2.notes.pop("sources_feedback", None)
    assert fb == "需要补充政策层面的源"
    s2.save()
    s3 = ProjectState.load(s.project_dir)
    assert "sources_feedback" not in s3.notes
    print("Test 3 passed: 驳回反馈存储/消费 OK")

# === Test 4: 全部 10 个 Stage 都能序列化 ===
for stage in Stage:
    with tempfile.TemporaryDirectory() as tmp:
        import research_agent.config as cfg
        cfg.PROJECTS_DIR = Path(tmp)
        s = ProjectState(topic="x", date_str="20260423", stage=stage)
        s.save()
        raw = json.loads(s.state_file.read_text(encoding="utf-8"))
        assert raw["stage"] == stage.value
print("Test 4 passed: 全部 10 个 Stage 序列化 OK")

# === Test 5: P3 循环字段（collect_round / converged / last_feedback_path） ===
with tempfile.TemporaryDirectory() as tmp:
    import research_agent.config as cfg
    cfg.PROJECTS_DIR = Path(tmp)

    s = ProjectState(topic="行业C", date_str="20260423")
    assert s.collect_round == 0
    assert s.max_collect_rounds == 3
    assert s.converged is False
    assert s.last_feedback_path is None
    assert s.validation_report_path is None

    # 模拟第 1 轮采集-验证
    s.advance_to(Stage.COLLECTING_AND_VALIDATING)
    s.collect_round = 1
    s.last_feedback_path = "/tmp/feedback_round_1.json"
    s.converged = False
    s.save()

    s2 = ProjectState.load(s.project_dir)
    assert s2.collect_round == 1
    assert s2.last_feedback_path == "/tmp/feedback_round_1.json"
    assert s2.converged is False

    # 模拟第 2 轮收敛
    s2.collect_round = 2
    s2.converged = True
    s2.sources_final_path = "/tmp/sources_final.md"
    s2.validation_report_path = "/tmp/validation_report.md"
    s2.save()

    s3 = ProjectState.load(s.project_dir)
    assert s3.collect_round == 2
    assert s3.converged is True
    assert s3.sources_final_path == "/tmp/sources_final.md"
    assert s3.validation_report_path == "/tmp/validation_report.md"

    # 模拟驳回重置
    s3.collect_round = 0
    s3.converged = False
    s3.last_feedback_path = None
    s3.advance_to(Stage.COLLECTING_AND_VALIDATING)
    assert s3.collect_round == 0
    assert s3.converged is False
    print("Test 5 passed: P3 循环字段持久化 & 重置 OK")

print("\n✓ 状态机冒烟测试全部通过")
