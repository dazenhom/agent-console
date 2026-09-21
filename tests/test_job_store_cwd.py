"""run_logged_oneshot 的 cwd 透传 + goal_verifier._build_prompt 的 workdir 拼接。

背对背验收隔离 worktree 子任务时，子进程必须在 worktree 目录下运行、prompt 里也要写明
该目录，否则评委在服务器默认 cwd 下核实产出会假阴性（真机复现的 bug）。这里 mock 掉
create_subprocess_exec，只验证 cwd 参数被正确透传，不真的起子进程。协程用 asyncio.run 驱动。
"""
import asyncio

from server import job_store, goal_verifier


class _FakeProc:
    """够用即可的假子进程：按新交互面（wait + stdout/stderr None）立即完成。

    pid 取超过 Linux pid_max 上限的值，os.killpg 必抛 ProcessLookupError 被捕获，
    测试绝不误伤任何真实进程组。
    """
    pid = 999999999
    returncode = 0
    stdout = None
    stderr = None

    async def wait(self):
        return 0

    class _Transport:
        def close(self):
            pass

    _transport = _Transport()

    def kill(self):
        pass


def _patch_subprocess(monkeypatch, seen):
    async def fake_exec(*cmd, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return _FakeProc()

    monkeypatch.setattr(job_store.asyncio, "create_subprocess_exec", fake_exec)


def test_run_logged_oneshot_passes_cwd(temp_db, monkeypatch):
    seen = {}
    _patch_subprocess(monkeypatch, seen)
    asyncio.run(job_store.run_logged_oneshot(
        "test", ["true"], timeout=5, cwd="/tmp/data/worktrees/xxx"))
    assert seen["cwd"] == "/tmp/data/worktrees/xxx"


def test_run_logged_oneshot_default_cwd_none(temp_db, monkeypatch):
    # 回归：不传 cwd 时应为 None，保持继承服务器默认工作目录的原行为
    seen = {}
    _patch_subprocess(monkeypatch, seen)
    asyncio.run(job_store.run_logged_oneshot("test", ["true"], timeout=5))
    assert seen["cwd"] is None


def test_build_prompt_includes_workdir():
    wd = "/tmp/data/worktrees/hello-txt-46f307"
    prompt = goal_verifier._build_prompt("目标", "标准", "产出", workdir=wd)
    assert wd in prompt
    assert "核实产出" in prompt


def test_build_prompt_without_workdir_backward_compatible():
    # 回归：不传 workdir 时 prompt 不含工作目录行，与改动前逐字一致
    prompt = goal_verifier._build_prompt("目标", "标准", "产出")
    assert "【工作目录】" not in prompt


def test_attempt_success_with_grandchild_holding_pipe():
    # 回归：codex 退出后 git 孙进程持有 stdout 管道写端，旧 communicate() 等管道
    # EOF 必超时丢结果；完成判据改为进程退出后必须立即成功拿到输出。
    async def run():
        return await job_store._attempt(
            ["bash", "-c", "echo RESULT; sleep 5 & exit 0"], timeout=3.0, cwd=None)

    out, err, status, error = asyncio.run(run())
    assert status == "success"
    assert "RESULT" in out
