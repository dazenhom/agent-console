"""背对背双执行 + 综合仲裁。

同一问题背对背交给两位"工程师"独立作答——A 走 Claude（tclaude），B 走 Codex（tcodex）——
互不知道对方的答案；随后把两版方案拼进仲裁 prompt，用更强的 Claude 模型指出分歧、给出
综合最优解。三次调用都是一次性子进程（复用 job_store.run_logged_oneshot），绝不碰会话
常驻上下文，与 goal_verifier / triage 一脉相承。

任一方案生成失败不中断另一路：失败方写占位文本，仲裁照常进行（让仲裁者据此判断）。
"""
import asyncio
import json

from . import config, db
from .job_store import run_logged_oneshot


async def _run_claude_oneshot(prompt: str, model: str, effort: str = "high",
                              kind: str = "arbitration") -> tuple[str, str]:
    """一次性调用 tclaude，返回 (job_id, 答案文本)。解析方式与 goal_verifier.verify 一致：
    从 stdout 逐行挑出 type=result 的 JSON 取 result 文本。失败/超时/无输出返回空文本。"""
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", model, "--output-format", "json",
        "--effort", effort,
    ]
    jid, text, stderr_text, status = await run_logged_oneshot(
        kind, cmd, config.ARBITRATION_TIMEOUT,
        model=model, input_summary=prompt[:120],
    )
    if status in ("timeout", "error"):
        return jid, ""
    result = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "result" and not data.get("is_error"):
            result = (data.get("result") or "").strip()
            break
    if result:
        db.set_job_output(jid, result[:500])
    return jid, result


async def _run_codex_oneshot(prompt: str, model: str) -> tuple[str, str]:
    """一次性调用 tcodex exec，返回 (job_id, 答案文本)。

    命令拼法与 codex_runner._build_cmd 一致（无 resume）；stdout 是 JSONL 事件流，
    最终答案来自 item.completed 事件里 item.type == 'agent_message' 的 text 字段
    （见 codex_runner._read_stdout 的解析逻辑）。取所有 agent_message 拼接，末条即最终答复。"""
    cmd = [config.CODEX_BIN, "--", "exec", "--json"]
    if config.CODEX_SKIP_GIT_CHECK:
        cmd += ["--skip-git-repo-check"]
    if config.CODEX_BYPASS:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd += ["-s", config.CODEX_SANDBOX]
    m = model or config.CODEX_MODEL
    if m:
        cmd += ["-m", m]
    cmd += [prompt]

    jid, text, stderr_text, status = await run_logged_oneshot(
        "arbitration_b", cmd, config.ARBITRATION_TIMEOUT,
        model=m, input_summary=prompt[:120],
    )
    if status in ("timeout", "error"):
        return jid, ""
    # 逐行解析 JSONL，收集 agent_message 文本（与 codex_runner 对 item.completed 的判断同源）
    messages: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item") or {}
            if item.get("type") == "agent_message":
                txt = (item.get("text") or "").strip()
                if txt:
                    messages.append(txt)
    result = "\n\n".join(messages).strip()
    if result:
        db.set_job_output(jid, result[:500])
    return jid, result


def _build_arbitration_prompt(question: str, model_a: str, result_a: str,
                              model_b: str, result_b: str) -> str:
    return (
        "以下是两位工程师对同一问题的独立方案。请指出他们的分歧点，并给出综合最优解。\n\n"
        f"【问题】\n{question}\n\n"
        f"【工程师A（Claude/{model_a}）的方案】\n{result_a}\n\n"
        f"【工程师B（Codex/{model_b}）的方案】\n{result_b}\n"
    )


async def run_arbitration(arb_id: str) -> None:
    """背对背跑 A/B 两版方案，再综合仲裁。整体 try 包裹，异常置 error。"""
    arb = db.get_arbitration(arb_id)
    if not arb:
        return
    question = arb.get("question") or ""
    # 阶段2 影子表：纯附加观测，写失败只记日志绝不影响仲裁主流程
    try:
        db.create_work_item(
            origin="arbiter", topology="candidates", isolation="shared",
            verify_mode="candidates", status="running",
            ref_id=arb_id, session_id=arb.get("session_id") or "", summary=question,
        )
    except Exception as e:
        print(f"[work_items] arbiter insert failed: {type(e).__name__}: {e}")
    model_a = arb.get("model_a") or config.CLAUDE_MODEL_SUPER
    model_b = arb.get("model_b") or (config.CODEX_MODEL or (config.CODEX_MODELS[0] if config.CODEX_MODELS else ""))
    try:
        # A/B 背对背并发；任一路异常/失败都不拖垮另一路（gather 用 return_exceptions）
        (res_a, res_b) = await asyncio.gather(
            _run_claude_oneshot(question, model_a, effort="high", kind="arbitration_a"),
            _run_codex_oneshot(question, model_b),
            return_exceptions=True,
        )
        if isinstance(res_a, Exception):
            job_a, text_a = "", f"（该方案生成失败：{res_a}）"
        else:
            job_a, text_a = res_a
            if not text_a:
                text_a = "（该方案生成失败：无输出）"
        if isinstance(res_b, Exception):
            job_b, text_b = "", f"（该方案生成失败：{res_b}）"
        else:
            job_b, text_b = res_b
            if not text_b:
                text_b = "（该方案生成失败：无输出）"

        db.update_arbitration(arb_id, result_a=text_a, job_a_id=job_a,
                              result_b=text_b, job_b_id=job_b)

        # 综合仲裁：走统一验收入口的 candidates 策略（内部仍用更强的 ARBITER_MODEL）
        from . import verifier
        job_final, verdict = await verifier.judge(
            "candidates", question=question, model_a=model_a, result_a=text_a,
            model_b=model_b, result_b=text_b,
        )
        if not verdict:
            verdict = "（仲裁生成失败：无输出）"
        db.update_arbitration(arb_id, verdict=verdict, job_final_id=job_final, status="done")
        try:
            db.update_work_item_status_by_ref(arb_id, "done")
        except Exception:
            pass
    except Exception as e:
        print(f"[arbiter] run_arbitration error: {type(e).__name__}: {e}")
        db.update_arbitration(arb_id, status="error", error=str(e))
        try:
            db.update_work_item_status_by_ref(arb_id, "error")
        except Exception:
            pass
