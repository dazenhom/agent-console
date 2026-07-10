"""
迁移脚本：为历史消息补全 parent 字段。

规则：按 created_at 顺序扫描每个会话的消息，
遇到 tool_use Agent 时入栈，入栈期间所有无 parent 的 tool_use/tool_result/assistant
都补上 parent = Agent.id；遇到该 Agent 对应的 tool_result 时出栈。
支持嵌套 Agent（Agent 里再起 Agent）。
"""
import sqlite3
import json
import sys

DB_PATH = "data/console.db"
DRY_RUN = "--dry-run" in sys.argv

conn = sqlite3.connect(DB_PATH)

# 取全部会话 id
session_ids = [r[0] for r in conn.execute("SELECT DISTINCT session_id FROM messages").fetchall()]
print(f"共 {len(session_ids)} 个会话")

total_updated = 0

for sid in session_ids:
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE session_id=? ORDER BY created_at",
        (sid,)
    ).fetchall()

    # stack of Agent tool_use ids，支持嵌套
    stack = []
    updates = []  # (new_content_json, row_id)

    for row_id, role, content_str in rows:
        try:
            c = json.loads(content_str)
        except Exception:
            continue

        if not isinstance(c, dict):
            continue

        # Agent tool_use：入栈
        if role == "tool_use" and c.get("name") == "Agent":
            agent_id = c.get("id")
            if agent_id:
                # 如果自己没有 parent 但当前有 stack，补 parent（嵌套 Agent）
                if stack and not c.get("parent"):
                    c["parent"] = stack[-1]
                    updates.append((json.dumps(c, ensure_ascii=False), row_id))
                stack.append(agent_id)
            continue

        # 非 Agent 消息：有 stack 且自身无 parent → 补 parent
        if stack and not c.get("parent"):
            # tool_result 可能是 Agent 本身的收尾
            if role == "tool_result" and c.get("tool_use_id") == stack[-1]:
                # 这是 Agent 的 tool_result，出栈，不补 parent（它不属于 Agent 内部）
                stack.pop()
                continue
            # 其他 tool_result/tool_use/assistant → 归属于栈顶 Agent
            c["parent"] = stack[-1]
            updates.append((json.dumps(c, ensure_ascii=False), row_id))
        elif stack and role == "tool_result" and c.get("tool_use_id") == stack[-1]:
            # 有 parent 但也是 Agent 收尾 → 出栈
            stack.pop()

    if updates:
        total_updated += len(updates)
        print(f"  session {sid}: 补 {len(updates)} 条")
        if not DRY_RUN:
            for new_content, row_id in updates:
                conn.execute("UPDATE messages SET content=? WHERE id=?", (new_content, row_id))
            conn.commit()

if DRY_RUN:
    print(f"\n[DRY RUN] 共需更新 {total_updated} 条消息，未写入。去掉 --dry-run 参数再跑一次即可。")
else:
    print(f"\n完成，共更新 {total_updated} 条消息。")

conn.close()
