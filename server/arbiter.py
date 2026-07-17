"""背对背双执行 + 综合仲裁。

同一问题背对背交给两位"工程师"独立作答——A 走 Claude（tclaude），B 走 Codex（tcodex）——
互不知道对方的答案；随后把两版方案拼进仲裁 prompt，用更强的 Claude 模型指出分歧、给出
综合最优解。三次调用都是一次性子进程（复用 job_store.run_logged_oneshot），绝不碰会话
常驻上下文，与 goal_verifier / triage 一脉相承。

任一方案生成失败不中断另一路：失败方写占位文本，仲裁照常进行（让仲裁者据此判断）。
"""
import asyncio
import json
import re

from . import config, db
from .job_store import run_logged_oneshot


async def _run_claude_oneshot(prompt: str, model: str, effort: str = "high",
                              kind: str = "arbitration",
                              cwd: str | None = None) -> tuple[str, str]:
    """一次性调用 tclaude，返回 (job_id, 答案文本)。解析方式与 goal_verifier.verify 一致：
    从 stdout 逐行挑出 type=result 的 JSON 取 result 文本。失败/超时/无输出返回空文本。

    cwd 透传给 run_logged_oneshot 作为子进程工作目录（None 时沿用默认，向后兼容）：
    背对背验收隔离 worktree 子任务时须传 worktree 目录，评委才能核实到正确的产出位置。"""
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", model, "--output-format", "json",
        "--effort", effort,
    ]
    jid, text, stderr_text, status = await run_logged_oneshot(
        kind, cmd, config.ARBITRATION_TIMEOUT,
        model=model, input_summary=prompt[:120], cwd=cwd,
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


async def _run_codex_oneshot(prompt: str, model: str,
                             cwd: str | None = None) -> tuple[str, str]:
    """一次性调用 tcodex exec，返回 (job_id, 答案文本)。

    命令拼法与 codex_runner._build_cmd 一致（无 resume）；stdout 是 JSONL 事件流，
    最终答案来自 item.completed 事件里 item.type == 'agent_message' 的 text 字段
    （见 codex_runner._read_stdout 的解析逻辑）。取所有 agent_message 拼接，末条即最终答复。

    cwd 透传给 run_logged_oneshot（None 时沿用默认，向后兼容）：背对背验收隔离 worktree
    子任务时须传 worktree 目录，评委才能核实到正确的产出位置。"""
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
    # 推理深度：非空时通过 -c model_reasoning_effort=<level> 传给 codex CLI。
    # 必须插在位置参数（prompt）之前。与 codex_runner._build_cmd 保持字面一致。
    if config.CODEX_REASONING_EFFORT:
        cmd += ["-c", f"model_reasoning_effort={config.CODEX_REASONING_EFFORT}"]
    cmd += [prompt]

    jid, text, stderr_text, status = await run_logged_oneshot(
        "arbitration_b", cmd, config.ARBITRATION_TIMEOUT,
        model=m, input_summary=prompt[:120], cwd=cwd,
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


def _broadcast_arb_stage(arb_id: str, stage: str) -> None:
    """向 monitor 通道推一条仲裁阶段进展（fire-and-forget，异常吞掉绝不影响仲裁主流程，
    写法与 scheduler._broadcast_goal_progress 一脉相承）。"""
    from .session_hub import hub
    try:
        asyncio.ensure_future(hub.broadcast_monitor({
            "type": "arbitration_progress", "arb_id": arb_id, "stage": stage,
        }))
    except Exception:
        pass


async def run_arbitration(arb_id: str) -> None:
    """背对背跑 A/B 两版方案，再综合仲裁。整体 try 包裹，异常置 error。
    全程分阶段打 stage 并广播 arbitration_progress，供前端实时点亮各卡片。"""
    arb = db.get_arbitration(arb_id)
    if not arb:
        return
    question = arb.get("question") or ""
    # 阶段2 影子表：纯附加观测，写失败只记日志绝不影响仲裁主流程
    _wid = db.create_work_item_safe(
        origin="arbiter", topology="candidates", isolation="shared",
        verify_mode="candidates", status="running",
        ref_id=arb_id, session_id=arb.get("session_id") or "", summary=question,
    )
    # 阶段4：把 work_item id 记回 arbitrations，建立 arbitration→work_item 的正向关联。
    # 单独兜底：回填失败同样只记日志，不影响仲裁主流程
    if _wid:
        try:
            db.update_arbitration(arb_id, work_item_id=_wid)
        except Exception as e:
            print(f"[work_items] arbiter backfill failed: {type(e).__name__}: {e}")
    model_a = arb.get("model_a") or config.CLAUDE_MODEL_SUPER
    model_b = arb.get("model_b") or (config.CODEX_MODEL or (config.CODEX_MODELS[0] if config.CODEX_MODELS else ""))
    try:
        # 进入 A/B 背对背阶段：先落 stage 再广播，前端据此把两张方案卡切到"生成中"
        db.update_arbitration(arb_id, stage="running_ab")
        _broadcast_arb_stage(arb_id, "running_ab")

        # A/B 各自跑完立即单独落库 + 广播各自 stage，让前端逐卡片点亮，不必等两路都完成。
        # 每路自带 try/except：任一路异常/无输出写占位文本，绝不拖垮另一路（沿用原降级语义）。
        async def _run_a() -> str:
            try:
                job_a, text_a = await _run_claude_oneshot(
                    question, model_a, effort="high", kind="arbitration_a")
                if not text_a:
                    text_a = "（该方案生成失败：无输出）"
            except Exception as e:
                job_a, text_a = "", f"（该方案生成失败：{e}）"
            db.update_arbitration(arb_id, result_a=text_a, job_a_id=job_a, stage="a_done")
            _broadcast_arb_stage(arb_id, "a_done")
            return text_a

        async def _run_b() -> str:
            try:
                job_b, text_b = await _run_codex_oneshot(question, model_b)
                if not text_b:
                    text_b = "（该方案生成失败：无输出）"
            except Exception as e:
                job_b, text_b = "", f"（该方案生成失败：{e}）"
            db.update_arbitration(arb_id, result_b=text_b, job_b_id=job_b, stage="b_done")
            _broadcast_arb_stage(arb_id, "b_done")
            return text_b

        text_a, text_b = await asyncio.gather(_run_a(), _run_b())

        # 两路都完成 → 进入综合仲裁阶段
        db.update_arbitration(arb_id, stage="arbitrating")
        _broadcast_arb_stage(arb_id, "arbitrating")

        # 综合仲裁：走统一验收入口的 candidates 策略（内部仍用更强的 ARBITER_MODEL）
        from . import verifier
        job_final, verdict = await verifier.judge(
            "candidates", question=question, model_a=model_a, result_a=text_a,
            model_b=model_b, result_b=text_b,
        )
        if not verdict:
            verdict = "（仲裁生成失败：无输出）"
        db.update_arbitration(arb_id, verdict=verdict, job_final_id=job_final,
                              status="done", stage="done")
        _broadcast_arb_stage(arb_id, "done")
        try:
            db.update_work_item_status_by_ref(arb_id, "done")
        except Exception:
            pass
    except Exception as e:
        print(f"[arbiter] run_arbitration error: {type(e).__name__}: {e}")
        db.update_arbitration(arb_id, status="error", error=str(e), stage="error")
        _broadcast_arb_stage(arb_id, "error")
        try:
            db.update_work_item_status_by_ref(arb_id, "error")
        except Exception:
            pass


def _parse_verdict(text: str) -> tuple[bool, str]:
    """把一路评委的原始答复解析成 (done, reason)。解析规则与 goal_verifier.verify 一致：
    取首个非空行精确匹配 == "DONE" 才算完成（容忍前后空白、大小写），像 "DONE, but…" 这类
    带尾巴的一律当 CONTINUE；从第二行起为判断理由，压平空白后截断。空文本按 CONTINUE 处理。"""
    lines = (text or "").splitlines()
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    done = first.upper() == "DONE"
    reason = re.sub(r"\s+", " ", " ".join(lines[1:])).strip()[:200]
    if not reason:
        reason = "已达成完成标准" if done else "尚未达成或无输出，按未完成处理"
    return done, reason


async def verify_back_to_back(goal: str, stop: str, produced: str,
                              git_diff: str, session_id: str | None = None,
                              workdir: str | None = None) -> tuple[bool, str]:
    """困难 dispatch 子任务的背对背双评委验收：复用 goal_verifier 的验收 prompt，交给两位
    评委独立判定——A 走更强的 Claude（tclaude），B 走 Codex（tcodex）——再合议。

    合议规则：两位评委都判 DONE 才算 done（done = done_a and done_b），任一 CONTINUE / 异常 /
    超时 / 无输出都当未完成，宁可多迭代也不误判完成。返回 (done, reason)，reason 把两位评委各自
    的判断理由都写进去便于排查。与 run_arbitration 一脉相承：两路一次性子进程并发（gather
    return_exceptions），任一路异常不拖垮另一路；不创建 arbitration 记录、不写 work_item。

    workdir 为子任务实际执行目录（隔离 worktree 时即 worktree 目录）：非空时既写进验收 prompt
    告知评委去哪核实，也作为两位评委子进程的 cwd，避免评委在服务器默认目录下核实产出而对隔离
    worktree 里正确完成的任务产生假阴性；None 时保持原行为。"""
    from . import goal_verifier
    prompt = goal_verifier._build_prompt(goal, stop, produced, git_diff=git_diff, workdir=workdir)
    model_b = config.CODEX_MODEL or (config.CODEX_MODELS[0] if config.CODEX_MODELS else "")
    (res_a, res_b) = await asyncio.gather(
        _run_claude_oneshot(prompt, config.CLAUDE_MODEL_SUPER, effort="high",
                            kind="dispatch_verify_a", cwd=workdir or None),
        _run_codex_oneshot(prompt, model_b, cwd=workdir or None),
        return_exceptions=True,
    )
    # 任一路异常/无输出按 CONTINUE(未完成) 处理，绝不误判完成
    if isinstance(res_a, Exception):
        done_a, reason_a = False, f"评委A异常：{res_a}"
    else:
        _job_a, text_a = res_a
        done_a, reason_a = _parse_verdict(text_a)
    if isinstance(res_b, Exception):
        done_b, reason_b = False, f"评委B异常：{res_b}"
    else:
        _job_b, text_b = res_b
        done_b, reason_b = _parse_verdict(text_b)

    done = done_a and done_b
    reason = (f"评委A(claude): {'DONE' if done_a else 'CONTINUE'} - {reason_a}；"
              f"评委B(codex): {'DONE' if done_b else 'CONTINUE'} - {reason_b}")
    return done, reason[:400]
