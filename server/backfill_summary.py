"""回填历史会话的标题/行摘要。

背景：84% 的历史 line_summary 调用超时走了启发式兜底（旧版取 AI 回复最后一句），
留下一批「新任务」占位标题和残句摘要，会话列表读不出这条会话在干什么。本脚本离线
重刷这些会话：取材与写回完全对齐在线路径（session_hub._build_title_convo 取材 +
summarizer.summarize_and_title 生成 + title_auto 双重复查），串行限速打 LLM 网关，
绝不覆盖用户手动改名（title_auto=0）的会话。

用法（在 agent-console 仓库根目录执行）：
  python3 -m server.backfill_summary --dry-run            # 只打印「sid: 旧标题 → 新标题」，不写库
  python3 -m server.backfill_summary --limit 3 --dry-run  # 先小批量试跑
  python3 -m server.backfill_summary                      # 真实回填（写库；由 ops/用户阶段执行）
"""
import argparse
import asyncio

from . import db
from .session_hub import hub
from .summarizer import summarize_and_title

# 串行限速：相邻两次 LLM 调用的最小间隔（脚本全程单协程），避免打爆网关
_INTERVAL_SEC = 2.0


def _candidates(limit: int) -> list[dict]:
    """待回填会话：AI 自动标题（title_auto=1）、未归档、不在跑，且标题是占位符
    （新任务/新会话/空）或截断残句（以 … 结尾）、或行摘要为空。
    title_auto=0（用户手动改名）绝不进列表——硬保护第一道。"""
    out = []
    for s in db.list_sessions():  # 默认排除归档与秘书会话
        if not s.get("title_auto", 1):
            continue
        if s.get("status") == "running":
            continue
        title = (s.get("title") or "").strip()
        if (title in ("新任务", "新会话") or title.endswith("…") or not title
                or not (s.get("summary") or "").strip()):
            out.append(s)
    return out[:limit] if limit > 0 else out


def _last_turn_texts(sid: str) -> tuple[str, str]:
    """取最近一轮的 user/assistant 文本，对齐在线路径 _summarize_and_emit 的入参口径
    （user_text=刚结束回合的指令，reply_text=该回合的 AI 回复）。"""
    user_text = reply_text = ""
    for m in reversed(db.list_messages(sid)):
        txt = str((m.get("content") or {}).get("text", "")).strip()
        if not txt:
            continue
        if m["role"] == "user" and not user_text:
            user_text = txt
        elif m["role"] == "assistant" and not reply_text:
            reply_text = txt
        if user_text and reply_text:
            break
    return user_text, reply_text


async def _run(dry_run: bool, limit: int) -> None:
    db.init_db()
    cands = _candidates(limit)
    total = len(cands)
    print(f"待回填会话 {total} 条（{'dry-run' if dry_run else '写库'}，串行限速 {_INTERVAL_SEC}s）")
    done = skipped = 0
    for i, s in enumerate(cands, 1):
        sid = s["id"]
        old_title = (s.get("title") or "").strip() or "（空）"
        try:
            convo = hub._build_title_convo(sid)
            user_text, reply_text = _last_turn_texts(sid)
            if not (convo or user_text or reply_text):
                print(f"[{i}/{total}] {sid}: 无消息可取材，跳过")
                skipped += 1
                continue
            # 带原标题进 prompt：占位/残句标题会触发 _PROMPT_TITLE_PARA 的"必须重写"分支
            title, summary = await summarize_and_title(convo, old_title, user_text, reply_text)
        except Exception as e:  # noqa: 单条失败不中断整批
            print(f"[{i}/{total}] {sid}: 生成失败 {type(e).__name__}: {e}")
            skipped += 1
            continue
        if not title and not summary:
            print(f"[{i}/{total}] {sid}: 模型未产出（标题/摘要均空），跳过")
            skipped += 1
        elif dry_run:
            print(f"[{i}/{total}] {sid}: {old_title} → {title or '(标题保持)'} | {summary}")
            done += 1
        else:
            # 写回前的硬保护：与在线 _summarize_and_emit 同款双重检查——await 期间
            # 用户可能手动改名（title_auto 翻 0）或会话被删，都不允许覆盖。
            fresh = db.get_session(sid)
            if not fresh:
                print(f"[{i}/{total}] {sid}: 会话已删除，跳过")
                skipped += 1
            else:
                fields: dict = {}
                if summary and summary != (fresh.get("summary") or ""):
                    fields["summary"] = summary
                if title and fresh.get("title_auto", 1) != 0 and title != (fresh.get("title") or ""):
                    fields["title"] = title
                    fields["title_auto"] = 1
                if fields:
                    # 回填是补写历史内容，不是新活动：_touch=False 别动 updated_at。
                    # 默认刷的话这些老会话会集体跳到列表最前面，打乱「最近更新」顺序（2026-09-22 已踩）。
                    db.update_session(sid, _touch=False, **fields)
                    print(f"[{i}/{total}] {sid}: {old_title} → {fields.get('title', '(标题保持)')}")
                    done += 1
                else:
                    print(f"[{i}/{total}] {sid}: 无变化，跳过")
                    skipped += 1
        if i < total:
            await asyncio.sleep(_INTERVAL_SEC)
    print(f"完成：回填 {done}，跳过 {skipped}，共 {total}")


def main() -> None:
    ap = argparse.ArgumentParser(description="回填历史会话的 AI 标题/行摘要")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印「sid: 旧标题 → 新标题」对照，不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条（默认全部）")
    args = ap.parse_args()
    asyncio.run(_run(args.dry_run, args.limit))


if __name__ == "__main__":
    main()
