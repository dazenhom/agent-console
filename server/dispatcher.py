"""角色化动态调度（Dispatcher）：把一个大需求拆成若干子任务，按确定性规则路由到
最合适的引擎/模型，并为每个子任务建隔离子会话后台开工。

与 triage 一脉相承：先用一次性子进程（最强 Claude）做"规划"输出 JSON 子任务列表，
解析同样走"可能包 ```json 代码块"的健壮处理；再由 _route 做**确定性**规则路由
（不依赖模型自由发挥，命中即止），最后建子会话 + fire-and-forget 跑 hub.start_turn。

设计原则：路由确定、失败隔离。规划失败/解析不到 → 空列表，什么都不建；单个子任务
建立失败不中断整个循环，记 status=error 跳过。session_hub 用函数内延迟 import
打破顶层循环导入。
"""
import asyncio
import json
import re

from . import config, db
from .job_store import run_logged_oneshot

_VALID_CATEGORIES = {"plan", "deep", "dev"}
_MAX_SUBTASKS = 8

# _route 的关键词表（命中即路由）。按 R1 > R2 > R3 顺序判定。
_KW_CODEX = ("独立视角", "交叉验证", "第二意见", "review", "复核", "审查", "对照", "double check")
_KW_DEEP = ("架构", "设计方案", "根因", "权衡", "推导", "算法", "重构", "为什么",
            "分析", "方案对比", "技术选型", "调研")
_KW_DEV = ("改", "修", "加字段", "写测试", "重命名", "格式化", "注释", "文档", "lint", "小改", "补充")


def _build_planner_prompt(request: str) -> str:
    request = (request or "").strip()[:3000]
    return (
        "你是一个资深技术负责人。下面是一个较大的开发需求。请把它拆解成若干个可独立执行的子任务，"
        "每个子任务足够聚焦、边界清晰，能单独交给一位工程师完成。\n\n"
        "输出格式要求（务必严格遵守）：\n"
        "- 只输出一个 JSON 数组，不要 markdown 代码块之外的任何前后缀说明。\n"
        '- 每项结构：{"title":"简短标题","instruction":"交给工程师的完整执行指令",'
        '"category":"plan|deep|dev","need_codex":true或false}\n'
        "- category：plan=需要先规划/调研，deep=需要深度分析/架构设计/根因推导，dev=直接改代码的开发活。\n"
        "- need_codex：若该子任务适合用另一套引擎做独立视角/交叉验证/复核，置 true，否则 false。\n"
        f"- 最多 {_MAX_SUBTASKS} 个子任务，宁少勿滥，粒度适中。\n\n"
        f"## 需求\n{request}\n"
    )


async def run_planner(request: str) -> list[dict]:
    """一次性子进程跑规划，返回校验后的子任务列表。失败/超时/解析不到 → []。"""
    prompt = _build_planner_prompt(request)
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", config.CLAUDE_MODEL_SUPER, "--output-format", "json",
        "--effort", "high",
    ]
    jid, text, stderr_text, status = await run_logged_oneshot(
        "dispatch_plan", cmd, config.ARBITRATION_TIMEOUT,
        model=config.CLAUDE_MODEL_SUPER, input_summary=(request or "")[:120],
    )
    if status in ("timeout", "error"):
        print(f"[dispatcher] run_planner: {status}")
        return []

    # 挑出 type=result 那行取 result（与 triage.run_triage 一致）
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
    if not result:
        print(f"[dispatcher] run_planner: no result, stderr={stderr_text[:200]!r}")
        return []

    # result 应是 JSON 数组；模型偶尔套 markdown 代码块，剥一下再解析
    result = re.sub(r"^```(?:json)?|```$", "", result.strip()).strip()
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        print(f"[dispatcher] run_planner: result not json: {result[:200]!r}")
        return []
    if not isinstance(parsed, list):
        # 容忍模型套一层 {"subtasks":[...]} / {"items":[...]}
        if isinstance(parsed, dict):
            parsed = parsed.get("subtasks") or parsed.get("items") or []
        if not isinstance(parsed, list):
            return []

    subtasks = []
    for it in parsed:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        instruction = (it.get("instruction") or "").strip()
        if not title and not instruction:
            continue
        category = (it.get("category") or "").strip().lower()
        if category not in _VALID_CATEGORIES:
            category = ""
        subtasks.append({
            "title": (title or instruction[:40])[:200],
            "instruction": instruction or title,
            "category": category,
            "need_codex": bool(it.get("need_codex")),
        })
        if len(subtasks) >= _MAX_SUBTASKS:
            break
    db.set_job_output(jid, "; ".join(s["title"] for s in subtasks) or "无子任务")
    return subtasks


def _route(subtask: dict) -> tuple[str, str, str]:
    """确定性路由：返回 (engine, model, category)。按 R1>R2>R3>R4 顺序，命中即止。"""
    title = subtask.get("title") or ""
    instruction = subtask.get("instruction") or ""
    text = f"{title}\n{instruction}".lower()
    category = subtask.get("category") or ""

    def _hit(keywords) -> bool:
        return any(kw.lower() in text for kw in keywords)

    codex_model = config.CODEX_MODELS[0] if config.CODEX_MODELS else config.CODEX_MODEL
    # R1：显式要 codex，或命中交叉验证/复核类关键词 → codex
    if subtask.get("need_codex") or _hit(_KW_CODEX):
        return "codex", codex_model, "codex"
    # R2：深度分析/架构/根因 → 强 Claude（Opus）
    if category == "deep" or _hit(_KW_DEEP):
        return "claude", "claude-opus-4-8", "deep"
    # R3：直接开发活 → Claude 强模型
    if category == "dev" or _hit(_KW_DEV):
        return "claude", config.CLAUDE_MODEL_STRONG, "dev"
    # R4 兜底
    return "claude", config.CLAUDE_MODEL_STRONG, "dev"


async def dispatch(request: str, parent_session_id: str | None, workdir: str) -> str:
    """规划 → 路由 → 建子会话 → 后台开工。返回 plan_id。

    session_hub 延迟 import 打破顶层循环导入。单个子任务建立失败记 status=error 跳过，
    不中断整个循环；子会话回合用 fire-and-forget 起跑，避免一个卡住其余子任务的建立。
    """
    from .session_hub import hub

    async def _fire(child_session_id: str, instruction: str, subtask_id: str):
        """后台跑 start_turn 并兜住其执行期异常：不然会被 asyncio 打成
        "Task exception was never retrieved"，子任务却永远卡在 dispatched。"""
        try:
            await hub.start_turn(child_session_id, instruction)
        except Exception as e:
            print(f"[dispatcher] start_turn error: {type(e).__name__}: {e}")
            db.update_dispatch_subtask(subtask_id, status="error")

    subtasks = await run_planner(request)
    plan_id = db.new_id()
    for seq, st in enumerate(subtasks):
        engine, model, category = _route(st)
        try:
            child = db.create_session(
                title=st["title"][:80], workdir=workdir, mode=model, engine=engine,
            )
            subtask = db.create_dispatch_subtask(
                plan_id=plan_id, parent_session_id=parent_session_id, seq=seq,
                title=st["title"], instruction=st["instruction"], category=category,
                engine=engine, model=model, child_session_id=child["id"],
                status="dispatched",
            )
            # 后台起跑，不 await 阻塞后续子任务的建立
            asyncio.ensure_future(_fire(child["id"], st["instruction"], subtask["id"]))
        except Exception as e:
            print(f"[dispatcher] dispatch subtask #{seq} error: {type(e).__name__}: {e}")
            db.create_dispatch_subtask(
                plan_id=plan_id, parent_session_id=parent_session_id, seq=seq,
                title=st.get("title", ""), instruction=st.get("instruction", ""),
                category=category, engine=engine, model=model,
                child_session_id="", status="error",
            )
    return plan_id
