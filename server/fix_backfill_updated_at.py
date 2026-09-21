"""恢复 2026-09-22 回填误刷的 43 条会话 updated_at（会话列表顺序被打乱）。

背景：当天用 server.backfill_summary 回填历史标题/摘要，写回走的是 db.update_session，
而它会无条件把 sessions.updated_at 刷成"现在"，于是这 43 条历史会话（真值最老的在 7 月）
集体瞬移到列表最前面，Overview / Sessions 的"最近更新"顺序被打乱。备份库
data/console.db.bak_20260922_prebackfill 里还留着被刷之前的真值，按白名单还原即可。

只改 updated_at 一个字段，且三道闸全过才改——防止把这期间真的产生过活动的会话改回旧值：
  1) sid 在回填日志白名单里（/tmp/backfill_20260922.log，解析条数必须正好 43）；
  2) 现值落在回填窗口 [01:20:00, 01:50:00]（窗口外说明之后被真实活动刷过）；
  3) 现库里该会话没有晚于备份 updated_at 的消息 / 任务记录（有 → 回填后确实跑过回合）。
预期结果 42 改 1 跳（d31f91e026ed 回填后有真实活动，保留现值）。

用法（在 agent-console 仓库根目录执行）：
  python3 -m server.fix_backfill_updated_at           # dry-run，只打印 sid 现值→备份值
  python3 -m server.fix_backfill_updated_at --apply   # 落盘（写前自动在线备份一份现库）
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import config

LOG_PATH = Path("/tmp/backfill_20260922.log")
_ENTRY_RE = re.compile(r"^\[\d+/43\] ([0-9a-f]{12}): ", re.M)

# 回填窗口：那次回填串行限速 2.0s、43 条约 1.4 分钟，给足余量。窗口外的现值一律不动。
WIN_LO = datetime(2026, 9, 22, 1, 20, 0).timestamp()
WIN_HI = datetime(2026, 9, 22, 1, 50, 0).timestamp()

# 判据 3 的容差：备份里 updated_at 通常略晚于同一回合最后一条消息/任务，允许 1s 抖动；
# 超过 1s 就认为回填之后确有新活动，绝不改。
_TOL = 1.0

_ACTION = "改"


def _parse_whitelist() -> list[str]:
    """从回填日志里取白名单。条数不对说明日志被截断/换了文件，此时宁可不改直接退出。"""
    if not LOG_PATH.exists():
        sys.exit(f"回填日志不存在：{LOG_PATH}（无法确认白名单，拒绝继续）")
    sids = _ENTRY_RE.findall(LOG_PATH.read_text(encoding="utf-8", errors="replace"))
    if len(sids) != 43:
        sys.exit(f"回填日志解析出 {len(sids)} 条 sid，预期 43 条（{LOG_PATH}），拒绝继续")
    return sids


def _backup_db_path() -> Path:
    """回填前的备份库：存着被刷之前的 updated_at 真值。"""
    return Path(config.DB_PATH).with_name("console.db.bak_20260922_prebackfill")


def _prefix_backup_path() -> Path:
    """本次修复前自动留的在线备份。"""
    return Path(config.DB_PATH).with_name("console.db.bak_20260922_prefix_updated_at")


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _plan(live: sqlite3.Connection, bak: sqlite3.Connection, sids: list[str]) -> list[tuple]:
    """逐条判定，返回 [(sid, 现值, 目标值, 判定/原因)]。判定为 _ACTION 的才会被写。"""
    rows = []
    for sid in sids:
        cur = live.execute("SELECT updated_at FROM sessions WHERE id=?", (sid,)).fetchone()
        if cur is None:
            rows.append((sid, None, None, "现库无此会话（已删除？）"))
            continue
        live_ts = cur[0] or 0
        old = bak.execute("SELECT updated_at FROM sessions WHERE id=?", (sid,)).fetchone()
        bak_ts = old[0] if old else None
        if not (WIN_LO <= live_ts <= WIN_HI):
            rows.append((sid, live_ts, bak_ts, "现值不在回填窗口内（之后被真实活动刷过）"))
            continue
        if bak_ts is None:
            rows.append((sid, live_ts, None, "备份库无此会话"))
            continue
        last_msg = live.execute(
            "SELECT max(created_at) FROM messages WHERE session_id=?", (sid,)
        ).fetchone()[0] or 0
        last_task = live.execute(
            "SELECT max(coalesce(ended_at, started_at)) FROM tasks WHERE session_id=?", (sid,)
        ).fetchone()[0] or 0
        if last_msg > bak_ts + _TOL or last_task > bak_ts + _TOL:
            rows.append((sid, live_ts, bak_ts,
                         f"回填后有新记录（消息 {last_msg:.1f} / 任务 {last_task:.1f} > 备份值+1s）"))
            continue
        if abs(live_ts - bak_ts) < 1e-6:
            rows.append((sid, live_ts, bak_ts, "现值已等于备份值"))
            continue
        rows.append((sid, live_ts, bak_ts, _ACTION))
    return rows


def _snapshot(src: Path, dst: Path) -> None:
    """在线备份：走 sqlite3 backup API 读源库（含 -wal 里还没 checkpoint 的写）。
    不能用 cp —— 直接拷主库文件会把 WAL 里的最新数据丢掉。"""
    s = _ro(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _apply(rows: list[tuple]) -> None:
    dst = _prefix_backup_path()
    _snapshot(Path(config.DB_PATH), dst)
    print(f"已在线备份现库 → {dst}")
    # 生产库是 WAL、两个 uvicorn 进程在写：短超时排队等锁，别直接把 "database is locked" 抛出去
    con = sqlite3.connect(config.DB_PATH, timeout=20)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        changed = skipped = 0
        for sid, live_ts, bak_ts, verdict in rows:
            if verdict != _ACTION:
                print(f"  skipped {sid}: {verdict}")
                skipped += 1
                continue
            # 窗口条件进 WHERE 作并发防护：万一这期间该会话被真实活动刷过，就不匹配、不改
            cur = con.execute(
                "UPDATE sessions SET updated_at=? WHERE id=? AND updated_at BETWEEN ? AND ?",
                (bak_ts, sid, WIN_LO, WIN_HI),
            )
            con.commit()
            if cur.rowcount == 1:
                print(f"  changed {sid}: {live_ts:.3f} → {bak_ts:.3f}")
                changed += 1
            else:
                print(f"  skipped {sid}: 并发保护拦下（updated_at 已不在回填窗口内）")
                skipped += 1
        print(f"完成：改 {changed}，跳 {skipped}，共 {len(rows)}")
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="恢复被回填刷掉的历史会话 updated_at")
    ap.add_argument("--apply", action="store_true", help="真实写库（默认只 dry-run 打印对照）")
    args = ap.parse_args()

    sids = _parse_whitelist()
    live = _ro(Path(config.DB_PATH))
    bak = _ro(_backup_db_path())
    try:
        rows = _plan(live, bak, sids)
    finally:
        live.close()
        bak.close()

    todo = [r for r in rows if r[3] == _ACTION]
    print(f"白名单 {len(sids)} 条，待改 {len(todo)} 条，跳过 {len(rows) - len(todo)} 条")
    for sid, live_ts, bak_ts, verdict in rows:
        if live_ts is None:
            print(f"  {sid}: {verdict}")
        elif verdict == _ACTION:
            print(f"  {sid}: {live_ts:.3f} → {bak_ts:.3f}  [{verdict}]")
        else:
            print(f"  {sid}: {live_ts:.3f} → {bak_ts}  [{verdict}]")

    if not args.apply:
        print("dry-run：未写库。确认无误后加 --apply 落盘。")
        return
    if not todo:
        print("没有需要修改的行，跳过写库。")
        return
    _apply(rows)


if __name__ == "__main__":
    main()
