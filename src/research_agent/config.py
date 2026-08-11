"""全局配置与常量。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# === LLM 配置（OpenAI 兼容 Chat Completions API）===
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_MAX_RETRIES: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))

# 向后兼容别名
DEFAULT_MODEL: str = LLM_MODEL

# === Web Search 配置（可选）===
SEARCH_API_PROVIDER: str = os.getenv("SEARCH_API_PROVIDER", "duckduckgo")
SEARCH_API_KEY: str = os.getenv("SEARCH_API_KEY", "")

# === 本地网页工作台访问控制 ===
# 绑定到非回环地址时必须提供令牌，否则任何人都能读取调研数据、改模型配置、删项目。
WEB_AUTH_TOKEN: str = os.getenv("WEB_AUTH_TOKEN", "")

# === Agent1 对话轮次上限 ===
STRATEGIST_MAX_ROUNDS: int = int(os.getenv("STRATEGIST_MAX_ROUNDS", "5"))

# === 路径 ===
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PROJECTS_DIR: Path = Path(os.getenv("PROJECTS_DIR", str(PROJECT_ROOT / "projects"))).expanduser()
SOURCE_DATA_DIR: Path = Path(os.getenv("SOURCE_DATA_DIR", str(PROJECT_ROOT / ".data" / "sources"))).expanduser()
PROJECT_SKILLS_DIR: Path = PROJECT_ROOT / "skills"

# === 产物文件名（每阶段固定） ===
FILE_OUTLINE = "01_outline.md"
# R1：研究开始阶段固定下来的必答问题清单，全流程共用同一组 question_id
FILE_RESEARCH_REQUIREMENTS = "research_requirements.json"
FILE_SOURCES_DRAFT = "02_sources_draft.md"
FILE_SOURCES_FINAL = "02_sources_final.md"
FILE_RAW_DATA_DIR = "03_raw_data"
FILE_VALIDATION = "03_validation_report.md"
FILE_ANALYSIS = "04_analysis.md"
FILE_ANALYSIS_OUTCOME = "04_analysis_outcome.json"
FILE_FINAL_REPORT = "05_final_report.md"
FILE_CHART_MANIFEST = "05_chart_manifest.json"
FILE_FINAL_REPORT_HTML = "05_final_report.html"
FILE_FINAL_REPORT_TEX = "05_final_report.tex"
FILE_FINAL_REPORT_PDF = "05_final_report.pdf"
FILE_STATE = "state.json"

# 循环中间产物命名（占位 {n}）
FILE_RAW_ROUND = "round_{n}.md"
FILE_FEEDBACK_ROUND = "feedback_round_{n}.json"

# === 循环上限 ===
MAX_COLLECT_ROUNDS: int = int(os.getenv("MAX_COLLECT_ROUNDS", "3"))
OUTPUT_PREFERENCE: str = os.getenv("OUTPUT_PREFERENCE", "balanced")

# === 券商研报排版 ===
REPORT_FORMATTING_SKILL: str = os.getenv(
    "REPORT_FORMATTING_SKILL", "brokerage-report-formatting"
)
REPORT_THEME: str = os.getenv("REPORT_THEME", "brokerage_research_v1")
REPORT_MAX_CHARTS: int = int(os.getenv("REPORT_MAX_CHARTS", "20"))
REPORT_ENABLE_LLM_CHART_FALLBACK: bool = os.getenv(
    "REPORT_ENABLE_LLM_CHART_FALLBACK", "true"
).lower() in {"1", "true", "yes", "on"}
REPORT_PANDOC_BIN: str = os.getenv("REPORT_PANDOC_BIN", "pandoc")
REPORT_LATEX_ENGINE: str = os.getenv("REPORT_LATEX_ENGINE", "xelatex")
REPORT_RENDER_TIMEOUT: int = int(os.getenv("REPORT_RENDER_TIMEOUT", "120"))

# === 检查点文件（用户确认后由 CLI 写入） ===
APPROVAL_MARK = ".approved"


def project_dir_for(topic: str, date_str: str) -> Path:
    """生成项目目录名：topic_YYYYMMDD。topic 中的路径分隔符被替换掉。"""
    safe_topic = topic.strip().replace("/", "_").replace("\\", "_")
    return PROJECTS_DIR / f"{safe_topic}_{date_str}"
