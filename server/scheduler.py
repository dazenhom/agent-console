"""定时/周期任务调度器。

不引 croniter（保持依赖闭包只有 fastapi/uvicorn/httpx）。只支持两种足够覆盖
「每小时查训练进度」类需求的规格：
  - interval：每 N 分钟跑一次
  - daily：每天 HH:MM 跑一次

后台 asyncio 循环每 ~30s tick：到点的 schedule → hub.start_turn（复用回合执行，
跑完自然走企业微信通知），重算 next_run。在 main 的 lifespan 启动里挂起。

时间用本地时区（服务器时间）。next_run 存 epoch 秒。
"""
import asyncio
import os
import signal
import time
from datetime import datetime, timedelta, date

from . import db, config

TICK_SEC = 30


def compute_next_run(kind: str, interval_min, at_hhmm, *, after: float | None = None) -> float | None:
    """算下一次运行的 epoch 秒。after 不传则用当前时间。"""
    now = after if after is not None else time.time()
    if kind == "goal":
        return now + config.GOAL_POLL_SEC
    if kind == "interval":
        try:
            m = int(interval_min)
        except (TypeError, ValueError):
            return None
        if m <= 0:
            return None
        return now + m * 60
    if kind == "daily":
        try:
            hh, mm = str(at_hhmm).split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            return None
        base = datetime.fromtimestamp(now)
        cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= now:
            cand = cand + timedelta(days=1)
        return cand.timestamp()
    return None


async def _run_loop():
    # 启动时给到点但 next_run 为空的补算一次（兼容手动改库）
    from .session_hub import hub
    while True:
        try:
            now = time.time()
            for sch in db.due_schedules(now):
                sid = sch["session_id"]
                sess = db.get_session(sid)
                if not sess:
                    # 会话已删 → 顺手禁用这条定时任务，避免空转
                    db.update_schedule(sch["id"], enabled=0)
                    continue
                if sch.get("kind") == "goal":
                    await _tick_goal(sch, sess, now)
                    continue
                # 已在跑就跳过本次（下个 tick 再看），避免堆叠
                if not hub.is_running(sid):
                    try:
                        await hub.start_turn(sid, sch["prompt"])
                    except Exception:
                        pass
                nxt = compute_next_run(sch["kind"], sch.get("interval_min"), sch.get("at_hhmm"), after=now)
                db.update_schedule(sch["id"], last_run=now, next_run=nxt)
            # dispatch 扇出的完成判定：与 schedule 遍历并列、共享同一 30s 节奏（dispatch 没有独立
            # schedule 记录，直接以 dispatch_subtasks 表为准）。内部只 ensure_future 起后台判定，
            # 不在本 tick await 任何会话/验收跑完，绝不阻塞其余调度处理。
            try:
                await _tick_fanout()
            except Exception:
                pass
        except Exception:
            pass
        await asyncio.sleep(TICK_SEC)


# ---------- 目标循环（kind=goal）状态机 ----------
# 每个 tick 只推进一步，转换先写库再动作（防重复触发）：
#   running   ─[会话空闲 & 未触顶]→ start_turn(迭代prompt), iter_count+1, goal_status=producing
#   producing ─[会话跑完]→ goal_status=verifying(先落库), ensure_future 后台 verify
#   verifying ─[后台任务完成]→ DONE→done / CONTINUE→running(存反馈)或 exhausted
#   done/exhausted：终态，enabled=0
# verifier 是 fire-and-forget（后台 ensure_future），绝不在 tick 主体 await——否则阻塞调度循环。

def _team_wrap(sch: dict, prompt: str) -> str:
    """exec_mode=='team' 时在最前面注入 /console-dev 四角流水线（复用 skill_store 的斜杠展开），
    让这一轮以 analyst→developer→reviewer→ops 的方式产出；solo（默认）原样返回，行为不变。"""
    if (sch.get("exec_mode") or "solo") == "team":
        from . import skill_store
        expanded, err = skill_store.expand("/console-dev " + prompt)
        if not err:
            return expanded
    return prompt


def _build_goal_prompt(sch: dict) -> str:
    """拼一轮迭代指令：目标 + 完成标准 +（有则）上轮验收反馈 + 轮次提示。

    exec_mode=='team' 时在最前面注入 /console-dev 四角流水线（复用 skill_store 的斜杠展开），
    让这一轮以 analyst→developer→reviewer→ops 的方式产出；solo（默认）走裸 prompt，行为不变。
    """
    iter_no = int(sch.get("iter_count") or 0) + 1
    parts = [
        "【目标】\n" + (sch.get("prompt") or "").strip(),
        "\n【完成标准】\n" + (sch.get("stop_condition") or "").strip(),
    ]
    feedback = (sch.get("last_feedback") or "").strip()
    if feedback:
        parts.append("\n【上一轮验收反馈，请针对性改进】\n" + feedback)
    parts.append(f"\n（这是第 {iter_no} 轮迭代，请朝完成标准推进，做完即可，不必啰嗦汇报。）")
    return _team_wrap(sch, "".join(parts))


async def _tick_goal(sch: dict, sess: dict, now: float) -> None:
    """按状态机推进一步。"""
    from .session_hub import hub
    scid = sch["id"]
    sid = sess["id"]
    status = sch.get("goal_status") or "running"

    # 新版 planned 模式（目标拆解为子任务，每轮委派一个）走独立状态机
    if (sch.get("goal_mode") or "flat") == "planned":
        await _tick_goal_planned(sch, sess, now)
        return

    # 终态：确保停用
    if status in ("done", "exhausted"):
        db.update_schedule(scid, enabled=0)
        return

    # verifying：后台验收任务在跑，本 tick 什么都不做，只顺延 next_run 避免反复被 due
    if status == "verifying":
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return

    # producing：等本轮会话跑完
    if status == "producing":
        if hub.is_running(sid):
            db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
            return
        # 会话已空闲 → 转 verifying（先落库再起后台任务，防重复触发）
        db.update_schedule(scid, goal_status="verifying",
                           next_run=compute_next_run("goal", None, None, after=now))
        asyncio.ensure_future(_run_goal_verify(scid))
        return

    # running：发起新一轮迭代（若未在跑、未触顶）
    if hub.is_running(sid):
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    iter_count = int(sch.get("iter_count") or 0)
    max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
    if iter_count >= max_iter:
        await _finish_goal(scid, sess, "exhausted", f"已达迭代上限（{max_iter} 轮）仍未完成")
        return
    # 成本熔断
    if config.GOAL_MAX_COST_USD > 0:
        # 已知局限（一期接受）：sum_session_cost 从 created_at 起算，会把用户在同一会话里的
        # 手动回合成本也计入目标循环预算——可能偏保守提前熔断。二期若要精确应按 goal 启动时间戳起算。
        spent = db.sum_session_cost(sid, sch.get("created_at") or 0)
        if spent >= config.GOAL_MAX_COST_USD:
            await _finish_goal(scid, sess, "exhausted",
                               f"已达成本上限（${spent:.2f} ≥ ${config.GOAL_MAX_COST_USD}）")
            return
    prompt = _build_goal_prompt(sch)
    try:
        await hub.start_turn(sid, prompt)
    except Exception:
        # 起回合失败：不推进状态，仅顺延 next_run，下个 tick 重试
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    db.update_schedule(scid, goal_status="producing", iter_count=iter_count + 1,
                       last_run=now, next_run=compute_next_run("goal", None, None, after=now))
    # 记录本轮迭代历史：start_turn 内已同步落 tasks 记录，此刻取回即为本轮任务
    # 阶段4：顺手关联 work_item（按 schedule id 反查影子记录），拿不到就留空，不影响主流程
    try:
        _wid = db.work_item_id_for_ref(scid) or ""
    except Exception:
        _wid = ""
    db.create_goal_iteration(scid, iter_count + 1, prompt, task_id=db.latest_task_id(sid) or "",
                             work_item_id=_wid)


async def _run_shell(command: str, cwd: str, timeout: float) -> str:
    """在 cwd 下跑一条 shell 命令，返回给 verifier 的结果摘要（退出码 + stdout/stderr 尾部）。

    command 是用户配置的字符串，直接交给 shell 执行——限定 cwd、加超时，只在会话工作区内跑，
    不额外解析。任何异常/超时都吞掉并如实描述，绝不让验收流程因命令失败而崩。
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as e:
        return f"[命令无法启动:{type(e).__name__}: {e}]"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # start_new_session=True 让 shell 成为进程组组长，超时须杀整个进程组，
        # 否则管道/后台任务/测试框架 fork 出的子孙进程会变孤儿继续跑
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            # 进程组可能已退出，兜底再杀一次 shell 本身
            try:
                proc.kill()
            except Exception:
                pass
        return f"[命令超时（>{timeout:.0f}s），已终止；按未通过对待]"
    stdout = (out.decode("utf-8", errors="replace") if out else "").strip()
    stderr = (err.decode("utf-8", errors="replace") if err else "").strip()
    # 只取尾部：测试/构建输出可能很长，结论多在末尾
    return (
        f"$ {command}\n退出码: {proc.returncode}\n"
        f"--- stdout(尾部) ---\n{stdout[-1200:]}\n"
        f"--- stderr(尾部) ---\n{stderr[-800:]}"
    )


async def _git_diff_stat(cwd: str) -> str:
    """worktree 会话取 git diff --stat 作为代码改动上下文；非 git 或失败返回空串。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--stat", cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return (out.decode("utf-8", errors="replace") if out else "").strip()
    except Exception:
        return ""


async def _gather_verify_context(sess: dict, verify_command: str | None = None) -> tuple[str, str, str]:
    """读取会话本轮验收上下文，返回 (produced, cmd_result, git_diff)。三处后台验收共用：
      produced    最新产出片段（会话 jsonl 尾部文本）
      cmd_result  仅当传入 verify_command 时在会话工作区跑一遍（退出码+输出尾部），否则空串
      git_diff    worktree 会话取 git diff --stat，否则空串
    只读上下文，不做任何判定/落库。"""
    from . import kanban
    produced = ""
    if sess.get("claude_session_id"):
        p = kanban._session_jsonl_path(sess["claude_session_id"], sess.get("workdir"))
        if p:
            produced = kanban._extract_recent_text(p)
    workdir = sess.get("workdir") or ""
    cmd_result = ""
    cmd = (verify_command or "").strip()
    if cmd and workdir:
        cmd_result = await _run_shell(cmd, workdir, config.GOAL_CMD_TIMEOUT)
    git_diff = ""
    if sess.get("is_worktree") and workdir:
        git_diff = await _git_diff_stat(workdir)
    return produced, cmd_result, git_diff


async def _run_goal_verify(scid: str) -> None:
    """后台验收本轮产出，是唯一把状态推回 running/done 的地方。
    整体 try/except 兜底：任何异常都复位 running，绝不让状态卡死在 verifying。"""
    from . import verifier
    try:
        sch = db.get_schedule(scid)
        if not sch or sch.get("goal_status") != "verifying":
            return
        sess = db.get_session(sch["session_id"])
        if not sess:
            db.update_schedule(scid, enabled=0)
            return
        # 读本轮验收上下文：产出片段 + 可执行验收命令结果（配了 verify_command 才跑）+ worktree 改动
        produced, cmd_result, git_diff = await _gather_verify_context(
            sess, verify_command=sch.get("verify_command"))
        done, reason = await verifier.judge("nl",
                                            goal=sch.get("prompt") or "",
                                            stop_condition=sch.get("stop_condition") or "",
                                            produced=produced,
                                            session_id=sess.get("id"), schedule_id=scid,
                                            cmd_result=cmd_result, git_diff=git_diff)
        iter_count = int(sch.get("iter_count") or 0)
        max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
        job = db.get_latest_job(scid, "goal_verify")
        if done:
            verdict, status = "done", "done"
        elif iter_count >= max_iter:
            verdict, status = "exhausted", "exhausted"
        else:
            verdict, status = "continue", "continue"
        iteration = db.get_goal_iteration_by_no(scid, iter_count)
        if iteration:
            # 存一份喂给 verifier 的内容摘要（产出+命令结果+改动），截断避免爆库
            excerpt = "\n\n".join(x for x in (produced, cmd_result, git_diff) if x)[:4000]
            db.update_goal_iteration(iteration["id"], verify_job_id=(job or {}).get("id") or "",
                                     verdict=verdict, feedback=reason, status=status,
                                     produced_excerpt=excerpt, ended_at=time.time())
        if done:
            await _finish_goal(scid, sess, "done", reason)
        elif iter_count >= max_iter:
            await _finish_goal(scid, sess, "exhausted",
                               f"已达迭代上限（{max_iter} 轮）：{reason}")
        else:
            db.update_schedule(scid, goal_status="running", last_feedback=reason)
    except Exception as e:
        db.update_schedule(scid, goal_status="running",
                          last_feedback=f"[verify异常:{type(e).__name__}]")
        sch = db.get_schedule(scid)
        iteration = db.get_goal_iteration_by_no(scid, int((sch or {}).get("iter_count") or 0))
        if iteration and iteration.get("status") == "producing":
            db.update_goal_iteration(iteration["id"], status="error",
                                     feedback=f"[verify异常:{type(e).__name__}]", ended_at=time.time())


async def _finish_goal(scid: str, sess: dict, status: str, reason: str) -> None:
    """落终态并停用，推企微 + 广播 monitor。"""
    from . import wecom_notify
    from .session_hub import hub
    db.update_schedule(scid, goal_status=status, enabled=0)
    # 阶段2 影子表：把对应影子记录收尾到 done/exhausted，写失败只记日志绝不影响主流程
    try:
        db.update_work_item_status_by_ref(scid, status)
    except Exception as e:
        print(f"[work_items] goal status update failed: {type(e).__name__}: {e}")
    title = (sess or {}).get("title") or "会话"
    head = "🎯 目标已达成" if status == "done" else "⏹️ 目标循环终止"
    asyncio.ensure_future(wecom_notify.notify(
        title=title,
        user_text=head,
        reply_text=reason,
        status="success" if status == "done" else "cancelled",
    ))
    try:
        asyncio.ensure_future(hub.broadcast_monitor({
            "type": "goal_update", "schedule_id": scid,
            "goal_status": status, "reason": reason,
        }))
    except Exception:
        pass


# ---------- 新版 planned 目标循环：目标拆解为子任务 → 每轮委派一个 → 单独验收 → 全做完再整体收尾 ----------
# 复用既有能力：拆解走 dispatcher.run_planner（子任务规划），委派走 hub.start_turn + exec_mode
# （team→console-dev 团队流水线），验收走 goal_verifier + verify_command（可执行验收）。
# plan_status 生命周期：'' →(起后台拆解)→ planning →(拆解落库)→ planned →(子任务全处理完)→
#   finalizing →(整体验收)→ 终态；拆解失败→plan_failed→exhausted。
# goal_status 在 planned 下沿用 running/producing/verifying：running=可派发下一子任务或收尾，
# producing=某子任务回合在跑，verifying=后台在验收（子任务级或收尾级，由 plan_status 区分）。

def _build_subtask_prompt(sch: dict, subtask: dict, done_cnt: int, total: int) -> str:
    """拼某个子任务的委派指令：总目标背景 + 当前子任务 +（重试时）上轮反馈 + 进度提示。
    exec_mode=='team' 时同样注入 /console-dev 流水线。"""
    parts = [
        "你在按计划分步推进一个较大的目标，现在只需专注完成【当前子任务】，做完即可，不必啰嗦汇报。",
        "\n【总目标】\n" + (sch.get("prompt") or "").strip(),
    ]
    stop = (sch.get("stop_condition") or "").strip()
    if stop:
        parts.append("\n【总目标完成标准】\n" + stop)
    parts.append(f"\n【当前子任务】（第 {done_cnt + 1}/{total} 个）{(subtask.get('title') or '').strip()}\n"
                 + (subtask.get("instruction") or "").strip())
    fb = (subtask.get("last_feedback") or "").strip()
    if fb:
        parts.append("\n【上一轮该子任务的验收反馈，请针对性改进】\n" + fb)
    return _team_wrap(sch, "".join(parts))


async def _tick_goal_planned(sch: dict, sess: dict, now: float) -> None:
    """planned 模式状态机：先拆解，再逐个子任务派发/验收，最后整体收尾。每 tick 只推进一步。"""
    from .session_hub import hub
    scid = sch["id"]
    sid = sess["id"]
    status = sch.get("goal_status") or "running"
    plan_status = sch.get("plan_status") or ""

    # 终态：确保停用
    if status in ("done", "exhausted"):
        db.update_schedule(scid, enabled=0)
        return

    # 拆解阶段：还没拆 → 起后台拆解；拆解中 → 等
    if plan_status in ("", "plan_failed"):
        if plan_status == "plan_failed":
            # 兜底：拆解失败态被再次调度（正常已 enabled=0），直接停用
            db.update_schedule(scid, enabled=0)
            return
        if hub.is_running(sid):  # 会话正忙（如手动回合）→ 让路，下个 tick 再拆
            db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
            return
        db.update_schedule(scid, plan_status="planning",
                           next_run=compute_next_run("goal", None, None, after=now))
        asyncio.ensure_future(_run_goal_plan(scid))
        return
    if plan_status == "planning":
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return

    # verifying：后台验收任务在跑（子任务级或收尾级），本 tick 只顺延
    if status == "verifying":
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return

    # producing：等本子任务回合跑完
    if status == "producing":
        if hub.is_running(sid):
            db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
            return
        db.update_schedule(scid, goal_status="verifying",
                           next_run=compute_next_run("goal", None, None, after=now))
        asyncio.ensure_future(_run_goal_verify_planned(scid))
        return

    # running：派发下一个子任务，或全做完后进整体收尾
    if hub.is_running(sid):
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    iter_count = int(sch.get("iter_count") or 0)
    max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
    if iter_count >= max_iter:
        await _finish_goal(scid, sess, "exhausted", f"已达迭代上限（{max_iter} 轮）仍未完成")
        return
    if config.GOAL_MAX_COST_USD > 0:
        spent = db.sum_session_cost(sid, sch.get("created_at") or 0)
        if spent >= config.GOAL_MAX_COST_USD:
            await _finish_goal(scid, sess, "exhausted",
                               f"已达成本上限（${spent:.2f} ≥ ${config.GOAL_MAX_COST_USD}）")
            return

    nxt = db.next_pending_goal_subtask(scid)
    if not nxt:
        # 子任务已全部处理完 → 整体收尾验收一次（用 verify_command + verifier 对总目标判定）
        db.update_schedule(scid, goal_status="verifying", plan_status="finalizing",
                           next_run=compute_next_run("goal", None, None, after=now))
        asyncio.ensure_future(_run_goal_verify_planned(scid))
        return

    subs = db.list_goal_subtasks(scid)
    total = len(subs)
    done_cnt = sum(1 for s in subs if s.get("status") in ("done", "skipped"))
    prompt = _build_subtask_prompt(sch, nxt, done_cnt, total)
    try:
        await hub.start_turn(sid, prompt)
    except Exception:
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    db.update_goal_subtask(nxt["id"], status="running", attempts=int(nxt.get("attempts") or 0) + 1)
    db.update_schedule(scid, goal_status="producing", active_subtask_id=nxt["id"],
                       iter_count=iter_count + 1, last_run=now,
                       next_run=compute_next_run("goal", None, None, after=now))
    # 阶段4：顺手关联 work_item（按 schedule id 反查），拿不到留空，不影响主流程
    try:
        _wid = db.work_item_id_for_ref(scid) or ""
    except Exception:
        _wid = ""
    db.create_goal_iteration(scid, iter_count + 1, prompt, task_id=db.latest_task_id(sid) or "",
                             work_item_id=_wid)


async def _run_goal_plan(scid: str) -> None:
    """后台把目标拆成有序子任务（复用 dispatcher.run_planner），落库后转 planned。
    拆不出任何子任务 → plan_failed 并按 exhausted 收尾；异常同样兜住不卡死。"""
    from . import dispatcher
    try:
        sch = db.get_schedule(scid)
        if not sch or sch.get("plan_status") != "planning":
            return
        goal = (sch.get("prompt") or "").strip()
        stop = (sch.get("stop_condition") or "").strip()
        request = goal + (f"\n\n【完成标准】\n{stop}" if stop else "")
        subtasks = await dispatcher.run_planner(request)
        # 再确认还在 planning（防重复 / 期间被禁用改动）
        sch = db.get_schedule(scid)
        if not sch or sch.get("plan_status") != "planning":
            return
        sess = db.get_session(sch["session_id"])
        if not sess:
            db.update_schedule(scid, enabled=0)
            return
        if not subtasks:
            db.update_schedule(scid, plan_status="plan_failed")
            await _finish_goal(scid, sess, "exhausted", "目标拆解失败：未能拆出任何可执行子任务")
            return
        # 阶段4：拆解落库的子任务顺手关联 work_item（按 schedule id 反查），拿不到留空
        try:
            _wid = db.work_item_id_for_ref(scid) or ""
        except Exception:
            _wid = ""
        db.replace_goal_subtasks(scid, subtasks, work_item_id=_wid)
        db.update_schedule(scid, plan_status="planned", goal_status="running",
                           next_run=compute_next_run("goal", None, None))
    except Exception as e:
        # 拆解异常：置 plan_failed 并收尾，避免 planning 态永久卡住反复起后台任务
        try:
            sch = db.get_schedule(scid)
            sess = db.get_session((sch or {}).get("session_id") or "")
            db.update_schedule(scid, plan_status="plan_failed")
            await _finish_goal(scid, sess or {}, "exhausted", f"目标拆解异常：{type(e).__name__}")
        except Exception:
            pass


async def _run_goal_verify_planned(scid: str) -> None:
    """planned 模式后台验收：plan_status=finalizing 时做整体收尾验收（含 verify_command），
    否则做当前子任务的进展验收。是唯一把 planned 状态推回 running/终态的地方，整体 try/except 兜底。"""
    from . import verifier
    try:
        sch = db.get_schedule(scid)
        if not sch or sch.get("goal_status") != "verifying":
            return
        sess = db.get_session(sch["session_id"])
        if not sess:
            db.update_schedule(scid, enabled=0)
            return
        iter_count = int(sch.get("iter_count") or 0)
        max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)

        # ---- 收尾整体验收：所有子任务已处理，用 verify_command + verifier 对总目标最终判定 ----
        if (sch.get("plan_status") or "") == "finalizing":
            # 收尾整体验收才跑 verify_command；子任务级验收不跑（见下方分支）
            produced, cmd_result, git_diff = await _gather_verify_context(
                sess, verify_command=sch.get("verify_command"))
            done, reason = await verifier.judge(
                "nl", goal=sch.get("prompt") or "", stop_condition=sch.get("stop_condition") or "",
                produced=produced,
                session_id=sess.get("id"), schedule_id=scid,
                cmd_result=cmd_result, git_diff=git_diff)
            # 收尾验收不对应任何新回合，不写/不覆盖 goal_iterations（最后一个子任务的轮次记录已定稿）
            db.update_schedule(scid, plan_status="finalized")
            if done:
                await _finish_goal(scid, sess, "done", reason)
            else:
                await _finish_goal(scid, sess, "exhausted",
                                   f"子任务已全部执行，但整体验收未通过：{reason}")
            return

        # ---- 子任务级进展验收（不跑 verify_command，仅 produced + worktree 改动）----
        produced, _cmd_result, git_diff = await _gather_verify_context(sess)
        sub_id = sch.get("active_subtask_id") or ""
        sub = db.get_goal_subtask(sub_id) if sub_id else None
        job = db.get_latest_job(scid, "goal_verify")
        iteration = db.get_goal_iteration_by_no(scid, iter_count)
        if not sub:
            # 兜底：找不到活动子任务，回 running 继续（下轮取下一个 pending）
            db.update_schedule(scid, goal_status="running", active_subtask_id="")
            return
        sub_goal = ((sub.get("title") or "") + "\n" + (sub.get("instruction") or "")).strip()
        sub_criteria = "完成上述子任务要求：" + (sub.get("instruction") or sub.get("title") or "")
        done, reason = await verifier.judge(
            "nl", goal=sub_goal, stop_condition=sub_criteria, produced=produced,
            session_id=sess.get("id"), schedule_id=scid, git_diff=git_diff)
        attempts = int(sub.get("attempts") or 0)
        if done:
            db.update_goal_subtask(sub["id"], status="done", last_feedback=reason,
                                   verify_job_id=(job or {}).get("id") or "")
            it_verdict, it_status = "done", "done"
        elif attempts >= config.GOAL_SUBTASK_MAX_ATTEMPTS:
            # 该子任务重试到上限仍未过 → 跳过，继续下一个，避免卡死
            db.update_goal_subtask(sub["id"], status="skipped", last_feedback=reason,
                                   verify_job_id=(job or {}).get("id") or "")
            it_verdict, it_status = "continue", "exhausted"
        else:
            # 未过且还有重试额度 → 回 pending，反馈喂给下一轮该子任务
            db.update_goal_subtask(sub["id"], status="pending", last_feedback=reason)
            it_verdict, it_status = "continue", "continue"
        if iteration:
            excerpt = "\n\n".join(x for x in (produced, git_diff) if x)[:4000]
            db.update_goal_iteration(iteration["id"], verify_job_id=(job or {}).get("id") or "",
                                     verdict=it_verdict, feedback=reason, status=it_status,
                                     produced_excerpt=excerpt, ended_at=time.time())
        if iter_count >= max_iter:
            await _finish_goal(scid, sess, "exhausted", f"已达迭代上限（{max_iter} 轮）：{reason}")
        else:
            db.update_schedule(scid, goal_status="running", active_subtask_id="", last_feedback=reason)
    except Exception as e:
        db.update_schedule(scid, goal_status="running", active_subtask_id="",
                           last_feedback=f"[verify异常:{type(e).__name__}]")
        sch = db.get_schedule(scid)
        iteration = db.get_goal_iteration_by_no(scid, int((sch or {}).get("iter_count") or 0))
        if iteration and iteration.get("status") == "producing":
            db.update_goal_iteration(iteration["id"], status="error",
                                     feedback=f"[verify异常:{type(e).__name__}]", ended_at=time.time())


# ---------- dispatch 扇出的完成判定：与 goal loop 同构的「验收 + 收尾」 ----------
# dispatch 把一个需求拆成 N 个子任务，各起一个 fire-and-forget 子会话去干活，本节补上判定：
#   dispatched ─[子会话跑完]→ verifying(先落库) + ensure_future 后台判定
#   verifying  ─[后台判定完成]→ done / failed（MVP 不重试，判定完直接终态）
#   error 保留原义：仅在 dispatcher 建子会话本身失败时打，本节不改其行为。
# 每个 plan 的所有子任务都到终态（done/failed/error）后，聚合收尾对应 work_item：
#   全 done → done，否则（有 failed/error）→ exhausted。
# 与 _run_goal_verify 一致，判定是 fire-and-forget（后台 ensure_future），绝不在 tick 主体 await。

async def _tick_fanout() -> None:
    """扫描所有还有未终结子任务的 dispatch plan，逐个推进子任务状态。每 tick 只推进一步。
    转换先写库再动作（先落 verifying 再起后台任务），防重复触发；本函数不 await 任何会话/验收。"""
    from .session_hub import hub
    for plan_id in db.list_active_dispatch_plans():
        for sub in db.list_dispatch_subtasks(plan_id):
            if sub.get("status") != "dispatched":
                continue
            sid = sub.get("child_session_id") or ""
            # 无子会话（不应发生）或子会话还在跑 → 跳过，下个 tick 再看
            if not sid or hub.is_running(sid):
                continue
            # 时序竞态防护：start_turn 是 fire-and-forget，从写 status='running' 到把 task
            # 注册进 hub._turns 之间有事件循环让出点，此刻 is_running 仍为 False，直接转 verifying
            # 会把"还没起跑"误判成"已跑完"。故再查最近一条 task：为空或仍 running 说明回合还没
            # 真正结束（没起跑 / 正在跑），留给下一拍；只有终态 task 才继续。
            task = db.get_latest_task(sid)
            if not task or task.get("status") == "running":
                continue
            # 子会话已空闲 → 转 verifying（先落库再起后台判定，防重复触发）
            db.update_dispatch_subtask(sub["id"], status="verifying")
            asyncio.ensure_future(_run_dispatch_verify(sub["id"]))


async def _run_dispatch_verify(subtask_id: str) -> None:
    """后台判定单个 dispatch 子任务是否完成，是唯一把 status 从 verifying 推向 done/failed 的地方。
    模式完全参考 _run_goal_verify：读子会话最新产出 → verifier.judge('nl',...) → 落 verdict/feedback。
    MVP 不重试：判定完直接 done 或 failed。整体 try/except 兜底，异常也落 failed，绝不卡在 verifying。
    无论走哪条分支，finally 都做一次 plan 级聚合收尾（幂等）。"""
    from . import verifier, arbiter
    sub = None
    try:
        sub = db.get_dispatch_subtask(subtask_id)
        if not sub or sub.get("status") != "verifying":
            return
        sess = db.get_session(sub.get("child_session_id") or "")
        if not sess:
            db.update_dispatch_subtask(subtask_id, status="failed", verdict="failed",
                                       feedback="子会话不存在，无法判定")
            return
        # 读子会话验收上下文（不跑 verify_command，丢弃 cmd_result）
        produced, _cmd_result, git_diff = await _gather_verify_context(sess)
        goal = ((sub.get("title") or "") + "\n" + (sub.get("instruction") or "")).strip()
        stop = "完成上述子任务要求：" + (sub.get("instruction") or sub.get("title") or "")
        # 困难任务走背对背双评委仲裁验收（两位评委都 DONE 才算完成），否则沿用单 judge。
        if sub.get("need_arbitration"):
            done, reason = await arbiter.verify_back_to_back(
                goal, stop, produced, git_diff, session_id=sess.get("id"))
        else:
            done, reason = await verifier.judge(
                "nl", goal=goal, stop_condition=stop, produced=produced,
                session_id=sess.get("id"), git_diff=git_diff)
        db.update_dispatch_subtask(subtask_id, status=("done" if done else "failed"),
                                   verdict=("done" if done else "failed"), feedback=reason)
    except Exception as e:
        db.update_dispatch_subtask(subtask_id, status="failed", verdict="failed",
                                   feedback=f"[verify异常:{type(e).__name__}]")
    finally:
        try:
            plan_id = (sub or db.get_dispatch_subtask(subtask_id) or {}).get("plan_id") or ""
            _maybe_finalize_fanout(plan_id)
        except Exception:
            pass


def _maybe_finalize_fanout(plan_id: str) -> None:
    """plan 下所有 subtask 都到终态（done/failed/error）后，聚合收尾对应 work_item：
    全 done → done，否则（有 failed/error）→ exhausted。未全终结则什么都不做。
    work_items 只作观测：这里只写它、不读它的字段做业务判断。全程吞异常不影响主流程。"""
    if not plan_id:
        return
    try:
        subs = db.list_dispatch_subtasks(plan_id)
        if not subs:
            return
        terminal = ("done", "failed", "error")
        if any((s.get("status") not in terminal) for s in subs):
            return
        status = "done" if all(s.get("status") == "done" for s in subs) else "exhausted"
        db.update_work_item_status_by_ref(plan_id, status)
    except Exception as e:
        print(f"[work_items] dispatch finalize failed: {type(e).__name__}: {e}")



_task = None
_sec_last: dict = {}  # report_type -> last run date string
_sec_task = None
_memo_last: dict = {}  # date -> 已触发标记
_memo_task = None


async def _secretary_loop():
    if not config.SECRETARY_ENABLED:
        return
    while True:
        try:
            now_dt = datetime.now()
            now_min = now_dt.hour * 60 + now_dt.minute
            today_str = date.today().isoformat()
            from . import secretary
            for report_type, time_cfg in [
                ("evening", config.SECRETARY_EVENING_TIME),
                ("morning", config.SECRETARY_MORNING_TIME),
            ]:
                cfg_h, cfg_m = map(int, time_cfg.split(":"))
                cfg_min = cfg_h * 60 + cfg_m
                if now_min >= cfg_min and _sec_last.get(report_type) != today_str:
                    _sec_last[report_type] = today_str
                    asyncio.ensure_future(secretary.run_report(report_type))
        except Exception:
            pass
        await asyncio.sleep(60)


async def _memo_loop():
    if not config.MEMO_REMIND_ENABLED:
        return
    from . import memo_reminder
    while True:
        try:
            rh, rm = map(int, config.MEMO_REMIND_TIME.split(":"))
            now_dt = datetime.now()
            now_min = now_dt.hour * 60 + now_dt.minute
            target_min = rh * 60 + rm
            today_str = date.today().isoformat()
            if now_min >= target_min and _memo_last.get("date") != today_str:
                _memo_last["date"] = today_str
                await memo_reminder.run_reminder()
        except Exception as e:
            print(f"[memo_loop] error: {e}")
        await asyncio.sleep(30)


def start():
    """在 FastAPI lifespan 里调用，挂起后台调度循环。"""
    # 只有显式设置 RUN_SCHEDULER 的进程才真正跑调度，避免双进程重复调度。
    if not os.environ.get("RUN_SCHEDULER"):
        return
    global _task, _sec_task, _memo_task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_loop())
    if _sec_task is None or _sec_task.done():
        _sec_task = asyncio.ensure_future(_secretary_loop())
    if _memo_task is None or _memo_task.done():
        _memo_task = asyncio.ensure_future(_memo_loop())
    return _task
