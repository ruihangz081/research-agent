"""流水线错误层级。

``PipelineError`` 是所有确定性门禁与阶段阻断错误的基类，``_safe_run`` 对它直接落
状态、不进入 ``MAX_RETRIES`` 次自动重试。

``DeterministicContentError`` 专用于「内容错误、重试无效」类失败——字形缺失、
ID 抄错、回填多字段这类错误重跑结果完全相同，每次自动重试都白烧约 2.8M prompt
token。把它们单独成类，是为了让 orchestrator 能统一走 ``except PipelineError``
直接落状态，而不是掉进 ``except Exception`` 被无意义重跑。

把 ``PipelineError`` 从 orchestrator 里拆到这个中性模块，是因为 ``sources/``
下的 ``TasksError`` 需要继承它，而 orchestrator 又反向导入 ``sources.tasks``——
若继续放在 orchestrator 里会形成导入环。
"""
from __future__ import annotations


class PipelineError(RuntimeError):
    """流水线阶段不可恢复的错误。"""


class DeterministicContentError(PipelineError):
    """确定性内容错误：重试无法改变结果，需修正上游产物。

    典型场景：PDF 字形缺失、引用/evidence ID 被 LLM 抄错或截断、任务回填字段
    越界。这些错误的根因在 Agent 已经写出的产物，而不是瞬态故障，自动重跑只会
    得到完全相同的结果。
    """


class PipelinePausedError(PipelineError):
    """流水线因外部条件（限流额度耗尽等）暂停，等待外部恢复。

    与 ``PipelineError`` 的区别：这是**可恢复的等待状态**，不是失败。重试无意义，
    也不应把项目标记为 failed；应进入 paused，等额度恢复后由用户显式 resume。
    """


# 错误信息里统一携带的提示，供工作台/日志一眼识别「重试无意义」。
DETERMINISTIC_CONTENT_HINT = "内容错误，重试无效，需修正上游产物"
