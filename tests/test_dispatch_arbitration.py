"""困难 dispatch 子任务的背对背仲裁验收：arbiter.verify_back_to_back 的合议规则、
scheduler._run_dispatch_verify 的分流（need_arbitration 走仲裁，否则走单 judge），以及
need_arbitration 字段在建库/迁移/重派中的默认与保留行为。

底层两位评委子进程（_run_claude_oneshot / _run_codex_oneshot）与 verifier.judge、worktree
等都用 monkeypatch 打桩，避免真的起子进程/调 LLM。协程用 asyncio.run 驱动。
"""
import asyncio

from server import arbiter, scheduler, db, verifier, dispatcher, worktree, job_store


# ---------------- arbiter.verify_back_to_back 合议 ----------------

def _stub_judges(monkeypatch, text_a=None, text_b=None, raise_a=False, raise_b=False):
    """打桩两位评委：分别返回给定文本，或抛异常（模拟超时/进程异常）。"""
    async def fake_claude(prompt, model, effort="high", kind="arbitration", cwd=None):
        if raise_a:
            raise RuntimeError("claude boom")
        return ("job-a", text_a)

    async def fake_codex(prompt, model, cwd=None):
        if raise_b:
            raise RuntimeError("codex boom")
        return ("job-b", text_b)

    monkeypatch.setattr(arbiter, "_run_claude_oneshot", fake_claude)
    monkeypatch.setattr(arbiter, "_run_codex_oneshot", fake_codex)


def _run_b2b():
    return asyncio.run(
        arbiter.verify_back_to_back("目标", "完成标准", "产出", git_diff="")
    )


def test_b2b_both_done(monkeypatch):
    _stub_judges(monkeypatch, text_a="DONE\nA说好了", text_b="DONE\nB也说好了")
    done, reason = _run_b2b()
    assert done is True
    assert "评委A(claude): DONE" in reason
    assert "评委B(codex): DONE" in reason


def test_b2b_one_continue_fails(monkeypatch):
    _stub_judges(monkeypatch, text_a="DONE\n好", text_b="CONTINUE\n还差点")
    done, reason = _run_b2b()
    assert done is False
    assert "CONTINUE" in reason


def test_b2b_one_exception_treated_as_continue(monkeypatch):
    # 一路异常/超时：另一路即使 DONE，也按未完成处理，绝不误判完成
    _stub_judges(monkeypatch, text_a="DONE\n好", raise_b=True)
    done, reason = _run_b2b()
    assert done is False
    assert "评委B异常" in reason


def test_b2b_empty_output_treated_as_continue(monkeypatch):
    # 一路无输出（空文本）→ 首个非空行不是 DONE → CONTINUE
    _stub_judges(monkeypatch, text_a="DONE\n好", text_b="")
    done, _reason = _run_b2b()
    assert done is False


def test_parse_verdict_rules():
    assert job_store.parse_done_verdict("DONE\n理由")[0] is True
    # 带尾巴不算 DONE
    assert job_store.parse_done_verdict("DONE, but not sure")[0] is False
    assert job_store.parse_done_verdict("CONTINUE\n还需改")[0] is False
    assert job_store.parse_done_verdict("")[0] is False
    # terra 档评委常先吐一条前导说明再给 verdict（多条 agent_message 被 \n\n 拼接）：
    # 扫描全文找首个独占 verdict 行，而不是只看首个非空行（后者会系统性误判 CONTINUE）
    done, reason = job_store.parse_done_verdict("我先核对…\n\nDONE\n理由")
    assert done is True and reason == "理由"
    assert job_store.parse_done_verdict("我先核对…\n\nCONTINUE\n理由")[0] is False
    # 全文没有任何独占 verdict 行时按 CONTINUE 兜底，绝不误判完成
    assert job_store.parse_done_verdict("我再想想\n没有结论")[0] is False


def test_b2b_workdir_into_prompt_and_cwd(monkeypatch):
    # 隔离 worktree 场景：workdir 既要写进两位评委的 prompt，也要作为 cwd 透传，
    # 否则评委在服务器默认目录下核实产出会假阴性（真机复现的 bug）。
    seen = {}

    async def fake_claude(prompt, model, effort="high", kind="arbitration", cwd=None):
        seen["prompt_a"], seen["cwd_a"] = prompt, cwd
        return ("job-a", "DONE\n好")

    async def fake_codex(prompt, model, cwd=None):
        seen["prompt_b"], seen["cwd_b"] = prompt, cwd
        return ("job-b", "DONE\n好")

    monkeypatch.setattr(arbiter, "_run_claude_oneshot", fake_claude)
    monkeypatch.setattr(arbiter, "_run_codex_oneshot", fake_codex)

    wd = "/tmp/data/worktrees/hello-txt-46f307"
    done, _reason = asyncio.run(
        arbiter.verify_back_to_back("目标", "完成标准", "产出", git_diff="", workdir=wd)
    )
    assert done is True
    assert seen["cwd_a"] == wd and seen["cwd_b"] == wd
    assert wd in seen["prompt_a"] and wd in seen["prompt_b"]


def test_b2b_no_workdir_backward_compatible(monkeypatch):
    # 回归：不传 workdir 时 cwd 为 None、prompt 里不含工作目录行，行为与改动前一致。
    seen = {}

    async def fake_claude(prompt, model, effort="high", kind="arbitration", cwd=None):
        seen["cwd_a"], seen["prompt_a"] = cwd, prompt
        return ("job-a", "DONE\n好")

    async def fake_codex(prompt, model, cwd=None):
        seen["cwd_b"] = cwd
        return ("job-b", "DONE\n好")

    monkeypatch.setattr(arbiter, "_run_claude_oneshot", fake_claude)
    monkeypatch.setattr(arbiter, "_run_codex_oneshot", fake_codex)

    asyncio.run(arbiter.verify_back_to_back("目标", "完成标准", "产出", git_diff=""))
    assert seen["cwd_a"] is None and seen["cwd_b"] is None
    assert "【工作目录】" not in seen["prompt_a"]


# ---------------- scheduler._run_dispatch_verify 分流 ----------------

def _seed_verifying_subtask(need_arbitration=0):
    """建一条 verifying 子任务，指向一个极简子会话（无 claude_session_id / 非 worktree，
    使 produced/git_diff 都为空，聚焦于验收分流本身）。"""
    child = db.create_session(title="子会话", workdir="/tmp", mode="m", engine="claude")
    plan_id = db.new_id()
    sub = db.create_dispatch_subtask(
        plan_id=plan_id, parent_session_id="parent", seq=0,
        title="子任务", instruction="干活", category="dev",
        engine="claude", model="strong", child_session_id=child["id"],
        status="verifying", need_arbitration=need_arbitration,
    )
    return sub["id"]


def test_verify_routes_to_arbiter_when_need_arbitration(temp_db, monkeypatch):
    calls = {}

    async def fake_b2b(goal, stop, produced, git_diff, session_id=None, workdir=None):
        calls["b2b"] = True
        calls["workdir"] = workdir
        return True, "两评委都 DONE"

    async def fake_judge(*args, **kwargs):
        calls["judge"] = True
        return False, "不该走到这"

    monkeypatch.setattr(arbiter, "verify_back_to_back", fake_b2b)
    monkeypatch.setattr(verifier, "judge", fake_judge)

    sub_id = _seed_verifying_subtask(need_arbitration=1)
    asyncio.run(scheduler._run_dispatch_verify(sub_id))

    assert calls.get("b2b") is True
    assert "judge" not in calls
    # 子会话 workdir（_seed_verifying_subtask 建库用的 /tmp）透传给背对背验收
    assert calls.get("workdir") == "/tmp"
    sub = db.get_dispatch_subtask(sub_id)
    assert sub["status"] == "done"
    assert sub["verdict"] == "done"


def test_verify_routes_to_judge_when_not_need_arbitration(temp_db, monkeypatch):
    # 回归：普通子任务验收路径完全不变，仍走单 judge
    calls = {}

    async def fake_b2b(*args, **kwargs):
        calls["b2b"] = True
        return True, "不该走到这"

    async def fake_judge(strategy, **kwargs):
        calls["judge"] = strategy
        return False, "尚未达成"

    monkeypatch.setattr(arbiter, "verify_back_to_back", fake_b2b)
    monkeypatch.setattr(verifier, "judge", fake_judge)

    sub_id = _seed_verifying_subtask(need_arbitration=0)
    asyncio.run(scheduler._run_dispatch_verify(sub_id))

    assert calls.get("judge") == "nl"
    assert "b2b" not in calls
    sub = db.get_dispatch_subtask(sub_id)
    assert sub["status"] == "failed"


# ---------------- need_arbitration 字段：默认 / 迁移 / 重派保留 ----------------

def test_subtask_defaults_zero(temp_db):
    # 不显式传 need_arbitration（模拟老库/旧调用），列默认 0 → 走普通验收路径
    plan_id = db.new_id()
    sub = db.create_dispatch_subtask(
        plan_id=plan_id, parent_session_id="p", seq=0,
        title="t", instruction="i", category="dev",
        engine="claude", model="strong", child_session_id="", status="dispatched",
    )
    assert db.get_dispatch_subtask(sub["id"])["need_arbitration"] == 0


def test_retry_preserves_need_arbitration(temp_db, monkeypatch):
    # 重派不碰 need_arbitration（update 白名单不含该列），困难标记应原样保留
    monkeypatch.setattr(worktree, "provision_workdir",
                        lambda base, hint, isolate: ("/tmp", "", 0, "", ""))
    monkeypatch.setattr(db, "create_session",
                        lambda **kw: {"id": "new-sess"})

    async def fake_fire(child_session_id, instruction, subtask_id):
        return None

    monkeypatch.setattr(dispatcher, "_fire_subtask", fake_fire)

    plan_id = db.new_id()
    sub = db.create_dispatch_subtask(
        plan_id=plan_id, parent_session_id="p", seq=0,
        title="t", instruction="i", category="dev",
        engine="claude", model="strong", child_session_id="old-sess",
        status="failed", verdict="failed", feedback="上次失败",
        base_workdir="/tmp", isolate=0, need_arbitration=1,
    )
    asyncio.run(dispatcher.retry_subtask(sub["id"]))

    updated = db.get_dispatch_subtask(sub["id"])
    assert updated["status"] == "dispatched"
    assert updated["verdict"] == ""      # 旧判定被清空
    assert updated["need_arbitration"] == 1  # 困难标记保留
