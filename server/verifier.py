"""统一验收/仲裁入口：把两类"判定"收进一个 judge(strategy, ...) 路由——

- "nl":         自然语言 DONE/CONTINUE 验收（原 goal_verifier.verify）。
- "candidates": 背对背双候选的综合仲裁（原 arbiter 的最终仲裁调用）。

本模块只做策略分发，不复制任何判定逻辑：各策略仍委托给原模块的实现，保证输出结果、
格式、落库字段逐字不变。goal_verifier / arbiter 用函数内延迟 import 打破循环导入。
"""
from . import config


async def judge(strategy: str, **kwargs):
    """按 strategy 路由到对应判定：

    - "nl":         委托 goal_verifier.verify，返回 (done, reason)。
                    kwargs 透传：goal, stop_condition, produced, session_id, schedule_id,
                    cmd_result, git_diff。
    - "candidates": 委托 arbiter 的仲裁 prompt + 一次性 Claude 调用（ARBITER_MODEL，high），
                    返回 (job_id, verdict)。kwargs：question, model_a, result_a, model_b, result_b。

    未知 strategy 抛 ValueError。
    """
    if strategy == "nl":
        from . import goal_verifier
        return await goal_verifier.verify(**kwargs)
    if strategy == "candidates":
        from . import arbiter
        prompt = arbiter._build_arbitration_prompt(
            kwargs["question"], kwargs["model_a"], kwargs["result_a"],
            kwargs["model_b"], kwargs["result_b"],
        )
        return await arbiter._run_claude_oneshot(
            prompt, config.ARBITER_MODEL, effort="high", kind="arbitration_final",
        )
    raise ValueError(f"unknown judge strategy: {strategy!r}")
