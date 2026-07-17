"""server.verifier.judge 的策略路由：nl / candidates / 未知策略。

judge 只做分发，底层 goal_verifier / arbiter 用 monkeypatch 打桩，避免真的调 LLM。
judge 是协程，测试里用 asyncio.run 驱动，故不引入 pytest-asyncio 依赖。
"""
import asyncio

import pytest

from server import verifier, goal_verifier, arbiter, config


def test_judge_nl_routes_to_goal_verifier(monkeypatch):
    captured = {}

    async def fake_verify(**kwargs):
        captured.update(kwargs)
        return ("done", "looks good")

    monkeypatch.setattr(goal_verifier, "verify", fake_verify)

    result = asyncio.run(
        verifier.judge("nl", goal="g", stop_condition="s", produced="p")
    )
    assert result == ("done", "looks good")
    # kwargs 原样透传给 goal_verifier.verify
    assert captured == {"goal": "g", "stop_condition": "s", "produced": "p"}


def test_judge_candidates_routes_to_arbiter(monkeypatch):
    calls = {}

    def fake_build(question, model_a, result_a, model_b, result_b):
        calls["build"] = (question, model_a, result_a, model_b, result_b)
        return "PROMPT"

    async def fake_oneshot(prompt, model, effort, kind):
        calls["oneshot"] = (prompt, model, effort, kind)
        return ("job-1", "verdict-x")

    monkeypatch.setattr(arbiter, "_build_arbitration_prompt", fake_build)
    monkeypatch.setattr(arbiter, "_run_claude_oneshot", fake_oneshot)

    result = asyncio.run(
        verifier.judge(
            "candidates",
            question="q", model_a="A", result_a="ra", model_b="B", result_b="rb",
        )
    )
    assert result == ("job-1", "verdict-x")
    assert calls["build"] == ("q", "A", "ra", "B", "rb")
    # 仲裁走 ARBITER_MODEL + high + arbitration_final，且 prompt 由 build 产出
    prompt, model, effort, kind = calls["oneshot"]
    assert prompt == "PROMPT"
    assert model == config.ARBITER_MODEL
    assert effort == "high"
    assert kind == "arbitration_final"


def test_judge_unknown_strategy_raises(monkeypatch):
    # 现状：未知 strategy 抛 ValueError（不静默兜底）。如实验证。
    with pytest.raises(ValueError):
        asyncio.run(verifier.judge("bogus"))
