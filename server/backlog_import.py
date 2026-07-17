"""把 quant-recommender 的 mandatory-backlog.md 接入 triage 收件箱。

与 triage.py 一脉相承：解析出的每条 backlog 事项落成一条 status='triage' 的收件箱条目
（source='backlog_import'，和秘书晚报的 source='triage' 分开），等人工在收件箱里 dispatch，
或在 scan 时按静态分类表自动派单。派单逻辑不重写，复用 triage._dispatch_existing_todo。

设计原则同 triage：默认保守。分类走硬编码静态表、不用 LLM；去重按 backlog_key(code) + content_hash；
内容变了但事项已派单（status != 'triage'）则只跳过并告警，绝不覆盖已在跑的任务。

注意：import triage 用函数内延迟 import，避免顶层循环导入（对齐 triage/scheduler 的做法）。
"""
import asyncio
import hashlib
import json
import re

from . import db

QUANT_REPO_ROOT = "/apdcephfs_gy2/share_302533218/zhihangxu/quant-recommender/"
BACKLOG_DOC_PATH = QUANT_REPO_ROOT + "docs/mandatory-backlog.md"

# 静态分类表：编号 -> (建议动作, 置信度)。硬编码，不用 LLM 判断。
_CLASSIFICATION = {
    "A1": ("auto", 0.9), "A2": ("auto", 0.9), "C1": ("auto", 0.85), "D2": ("auto", 0.85),
    "A3": ("triage", 0.6), "B1": ("triage", 0.5), "B3": ("triage", 0.5), "C2": ("triage", 0.4),
    "D1": ("triage", 0.4), "B2": ("triage", 0.4), "B4": ("triage", 0.4),
    "E1": ("triage", 0.3), "E2": ("triage", 0.3), "E3": ("triage", 0.3),
}
# 未在表里的编号默认保守兜底，不报错。
_DEFAULT_CLASSIFICATION = ("triage", 0.3)

_HEAD_RE = re.compile(r"^### ([A-E]\d+)\.\s+(.+?)\s*$")
_STATUS_RE = re.compile(r"^- 当前状态：(.+?)\s*$")
_GOAL_RE = re.compile(r"^- 目标：(.+?)\s*$")
_ACCEPT_HEAD_RE = re.compile(r"^- 验收：\s*$")
_SUB_RE = re.compile(r"^\s+-\s+(.+?)\s*$")


def parse_backlog(text: str) -> list[dict]:
    """状态机解析文档，输出 [{code, title, current_status, goal, acceptance:[...]}, ...]。"""
    items: list[dict] = []
    cur: dict | None = None
    in_acceptance = False
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        m = _HEAD_RE.match(line)
        if m:
            if cur:
                items.append(cur)
            cur = {"code": m.group(1), "title": m.group(2).strip(),
                   "current_status": "", "goal": "", "acceptance": []}
            in_acceptance = False
            continue
        if cur is None:
            continue
        # 遇到二级小节标题，结束当前验收子列表的收集
        if line.startswith("## "):
            in_acceptance = False
            continue
        ms = _STATUS_RE.match(line)
        if ms:
            cur["current_status"] = ms.group(1).strip()
            in_acceptance = False
            continue
        mg = _GOAL_RE.match(line)
        if mg:
            cur["goal"] = mg.group(1).strip()
            in_acceptance = False
            continue
        if _ACCEPT_HEAD_RE.match(line):
            in_acceptance = True
            continue
        if in_acceptance:
            msub = _SUB_RE.match(line)
            if msub:
                cur["acceptance"].append(msub.group(1).strip())
                continue
            # 缩进子列表以外的非空行结束验收段
            if line.strip():
                in_acceptance = False
    if cur:
        items.append(cur)
    return items


def _content_hash(item: dict) -> str:
    raw = item["title"] + item["current_status"] + item["goal"] + "\n".join(item["acceptance"])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _build_goal_prompt(item: dict) -> str:
    return (
        f"项目路径：{QUANT_REPO_ROOT}\n\n"
        f"任务【{item['code']} {item['title']}】\n"
        f"当前状态：{item['current_status']}\n"
        f"目标：{item['goal']}\n\n"
        "请在上述项目目录内完成该目标。完成的验收标准见 stop_condition。"
    )


def _build_stop_condition(item: dict) -> str:
    lines = ["全部满足以下验收标准才算完成："]
    for i, acc in enumerate(item["acceptance"], 1):
        lines.append(f"{i}. {acc}")
    return "\n".join(lines)


def _build_payload(item: dict, conf: float, action: str, chash: str) -> dict:
    """triage_payload 结构对齐 triage 现状约定，供 _dispatch_existing_todo 消费。"""
    return {
        "title": f"[{item['code']}] {item['title']}",
        "category": "chore",
        "confidence": conf,
        "action": action,
        "reason": "来自 mandatory-backlog.md 静态分类",
        "goal_prompt": _build_goal_prompt(item),
        "stop_condition": _build_stop_condition(item),
        "workdir": QUANT_REPO_ROOT,
        "backlog_key": item["code"],
        "content_hash": chash,
    }


def _load_existing() -> dict:
    """一次性拉出库里全部 source='backlog_import' 的 todos，Python 里按 backlog_key 建索引。"""
    rows = db._query("SELECT * FROM todos WHERE source=?", ("backlog_import",))
    existing: dict = {}
    for r in rows:
        todo = dict(r)
        try:
            payload = json.loads(todo.get("triage_payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        key = payload.get("backlog_key")
        if not key:
            continue
        existing[key] = {
            "todo": todo,
            "content_hash": payload.get("content_hash"),
            "status": todo.get("status"),
        }
    return existing


def _run_async(coro):
    """在无运行事件循环的线程里跑协程（scan_backlog 是同步函数，端点用 to_thread 调它）。"""
    return asyncio.run(coro)


def scan_backlog(auto_dispatch: bool = False) -> dict:
    """扫描 backlog 文档，增量落 triage 收件箱；auto_dispatch=True 时对新建的 auto 项直接派单。

    返回各类结果的 code 列表：created / updated / skipped / dispatched / failed。
    """
    with open(BACKLOG_DOC_PATH, encoding="utf-8") as f:
        text = f.read()
    items = parse_backlog(text)
    existing = _load_existing()

    result = {"created": [], "updated": [], "skipped": [], "dispatched": [], "failed": []}
    # 记录本次新建的 (code, tid, payload)，供 auto_dispatch 阶段派单
    created_dispatchable: list[tuple[str, str, dict]] = []

    for item in items:
        code = item["code"]
        conf_action = _CLASSIFICATION.get(code, _DEFAULT_CLASSIFICATION)
        action, conf = conf_action[0], conf_action[1]
        chash = _content_hash(item)
        full_title = f"[{code}] {item['title']}"
        payload = _build_payload(item, conf, action, chash)

        ex = existing.get(code)
        if ex is not None:
            if ex["content_hash"] == chash:
                result["skipped"].append(code)
                continue
            # 内容变了：只有还没派单（仍在 triage）才就地刷新，否则不动已在跑的任务
            if ex["status"] == "triage":
                db.update_todo(
                    ex["todo"]["id"],
                    title=full_title,
                    description=item["goal"],
                    confidence=conf,
                    suggested_action=action,
                    triage_payload=json.dumps(payload, ensure_ascii=False),
                )
                result["updated"].append(code)
            else:
                print(f"[backlog_import] WARNING: {code} 内容已变但状态为 "
                      f"{ex['status']!r}（已派单/已处理），跳过不覆盖")
                result["skipped"].append(code)
            continue

        # 新建：两步写入（create_triage_todo source 硬编码 'triage'，这里要 'backlog_import'）
        todo = db.create_todo(
            title=full_title, description=item["goal"], status="triage",
        )
        db.update_todo(
            todo["id"], source="backlog_import",
            confidence=conf, suggested_action=action,
            triage_payload=json.dumps(payload, ensure_ascii=False),
        )
        result["created"].append(code)
        created_dispatchable.append((code, todo["id"], payload))

    if auto_dispatch:
        from . import triage  # 延迟 import 破循环
        for code, tid, payload in created_dispatchable:
            if payload["action"] != "auto":
                continue
            try:
                ok = _run_async(triage._dispatch_existing_todo(tid, payload))
            except Exception as e:  # noqa: BLE001 派单失败降级记录，不中断整轮
                ok = False
                print(f"[backlog_import] dispatch {code} error: {type(e).__name__}: {e}")
            if ok:
                result["dispatched"].append(code)
            else:
                result["failed"].append(code)

    print(f"[backlog_import] scan done: created={result['created']} updated={result['updated']} "
          f"skipped={result['skipped']} dispatched={result['dispatched']} failed={result['failed']}")
    return result
