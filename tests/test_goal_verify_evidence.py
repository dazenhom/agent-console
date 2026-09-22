"""验收成本治理的回归测试。

P0：worktree 会话的 git_diff 必须能看到 base..HEAD 的已提交改动——agent 干完活会自己
commit，原来无 ref 的 `git diff --stat`（只看未提交改动）实测在三个真实 worktree 会话里
输出全为空，评委只能在零证据下自己跑 97 条 git 命令重建改动清单（验收成本 27.1 万 tok
vs 共享会话 11.5 万）。

P1：确定性证据包（verify_evidence.collect）+ prompt 引导语。证据包必须：有界（≤配置字节数，
超限落盘 + 给路径）、有界耗时、任何异常都降级为空串，绝不把验收主流程拖挂。
"""
import asyncio
import subprocess
import time
from pathlib import Path

from server import config, goal_verifier, scheduler, verify_evidence, worktree


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _isolated_worktree_with_commit(git_repo):
    """建一个隔离 worktree，在里面改一个文件并**提交**（模拟 agent 的真实收尾动作）。
    返回 (worktree 目录, base HEAD sha)。"""
    base_sha = _git(git_repo, "rev-parse", "HEAD").strip()
    wd, branch, is_wt, wt_base, notice, wt_sha = worktree.provision_workdir(
        git_repo, "cost-test", isolate=True)
    assert is_wt == 1 and wt_sha == base_sha and branch
    (Path(wd) / "report.md").write_text("done\n")
    _git(wd, "add", ".")
    _git(wd, "commit", "-q", "-m", "写报告")
    return wd, base_sha


def test_git_diff_stat_with_base_sha_sees_committed_changes(git_repo, worktrees_root):
    wd, base_sha = _isolated_worktree_with_commit(git_repo)

    # 现状（无 base sha）：改动已提交，无 ref 的 diff --stat 恒为空 —— 这正是生产失效的根因
    assert asyncio.run(scheduler._git_diff_stat(wd)) == ""

    # 修复后：给出 base..HEAD 的结构化摘要（提交列表 + stat + name-status + 未提交改动）
    out = asyncio.run(scheduler._git_diff_stat(wd, base_sha))
    assert f"[基线 {base_sha[:10]}..HEAD]" in out
    assert "写报告" in out                 # log --oneline 里有本轮提交
    assert "report.md" in out              # stat / name-status 里有改动文件
    assert "[未提交改动]" in out


def test_git_diff_stat_ignores_non_sha_base(git_repo, worktrees_root):
    # 脏数据/历史会话：base 不是 sha 形态 → 退回原行为（只看未提交改动），绝不拼进 git 命令
    wd, _ = _isolated_worktree_with_commit(git_repo)
    assert asyncio.run(scheduler._git_diff_stat(wd, "HEAD~1; rm -rf /")) == ""
    assert asyncio.run(scheduler._git_diff_stat(wd, "")) == ""


# ---------------- P1：确定性证据包 ----------------

def test_collect_returns_tree_and_keyword_hits(tmp_path):
    (tmp_path / "notes.md").write_text("结论：ALPHA_TOKEN_XYZ 已验证\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("print(1)\n", encoding="utf-8")

    t0 = time.monotonic()
    out = asyncio.run(verify_evidence.collect(str(tmp_path), "确认 ALPHA_TOKEN_XYZ 已生效"))
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0                                   # 有界耗时（内部预算 5s）
    assert len(out.encode("utf-8")) <= config.GOAL_SPILL_EVIDENCE_BYTES
    assert "【工作区文件树" in out and "notes.md" in out
    # 完成标准里的标识符要被检索出来（评委不用再自己 grep 一遍）
    assert "【完成标准关键词命中行】" in out and "ALPHA_TOKEN_XYZ" in out


def test_collect_skips_keyword_section_without_identifiers(tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert verify_evidence._keywords("完成这个任务") == []
    out = asyncio.run(verify_evidence.collect(str(tmp_path), "完成这个任务"))
    assert "关键词命中行" not in out


def test_collect_missing_or_empty_workdir_returns_empty():
    assert asyncio.run(verify_evidence.collect("/nope/does/not/exist", "标准")) == ""
    assert asyncio.run(verify_evidence.collect("", "标准")) == ""


def test_collect_never_raises_on_internal_failure(monkeypatch):
    # 采集内部炸掉也必须降级成空证据：验收主流程不能因为证据包失败而中断/误判
    def boom(*a, **kw):
        raise RuntimeError("采集炸了")

    monkeypatch.setattr(verify_evidence, "_collect_sync", boom)
    assert asyncio.run(verify_evidence.collect("/tmp", "标准")) == ""


def test_collect_oversize_spills_to_file(tmp_path, temp_db, monkeypatch):
    # 超预算：落盘 + 首尾预览 + 路径，返回值仍不超过预算（评委可自己捞全文）
    monkeypatch.setattr(config, "GOAL_SPILL_EVIDENCE_BYTES", 400)
    for i in range(30):
        (tmp_path / f"file_{i:02d}.txt").write_text(f"payload {i}\n" * 20, encoding="utf-8")
    out = asyncio.run(verify_evidence.collect(str(tmp_path), "检查 file_07 是否存在"))
    assert len(out.encode("utf-8")) <= 400
    assert "完整内容已存于文件" in out


def test_collect_previews_changed_report_file(git_repo, worktrees_root):
    wd, base_sha = _isolated_worktree_with_commit(git_repo)
    out = asyncio.run(verify_evidence.collect(wd, "确认 report.md 写完", base_sha))
    assert "【本轮改动文件摘要" in out
    assert "report.md" in out and "done" in out          # 报告类文件的首尾预览
    assert "【本轮提交与改动文件" in out


# ---------------- P1：接线与 prompt ----------------

def test_gather_verify_context_returns_evidence_and_passes_stop_condition(monkeypatch):
    seen = {}

    async def fake_collect(workdir, stop_condition, base_ref=""):
        seen.update(workdir=workdir, stop=stop_condition, base_ref=base_ref)
        return "EVID"

    monkeypatch.setattr(verify_evidence, "collect", fake_collect)
    sess = {"workdir": "/tmp/sess-dir", "is_worktree": 1, "worktree_base_sha": "abc1234"}
    produced, cmd_result, git_diff, evidence = asyncio.run(
        scheduler._gather_verify_context(sess, stop_condition="完成标准 SC"))

    assert evidence == "EVID"
    assert seen == {"workdir": "/tmp/sess-dir", "stop": "完成标准 SC", "base_ref": "abc1234"}
    assert cmd_result == ""       # 没配 verify_command 就不跑命令


def test_gather_verify_context_degrades_to_empty_evidence(monkeypatch):
    # 证据包拿不到（None/空）时不得影响另外三项上下文
    async def empty_collect(workdir, stop_condition, base_ref=""):
        return ""

    monkeypatch.setattr(verify_evidence, "collect", empty_collect)
    produced, cmd_result, git_diff, evidence = asyncio.run(
        scheduler._gather_verify_context({"workdir": "/tmp"}, stop_condition="x"))
    assert evidence == "" and produced == "" and git_diff == ""


def test_build_prompt_includes_evidence_section_and_guidance():
    prompt = goal_verifier._build_prompt(
        "目标", "标准", "产出", evidence="【工作区文件树】a.py 12B 09-22 10:00")
    assert "【已为你预先收集的工作区证据】" in prompt
    assert "a.py 12B" in prompt
    # 引导语：先用证据判断 + 别重复已提供的探查（只塞证据不引导不会减少自探）
    assert "无需再用 shell 重新获取" in prompt
    assert "不要重复已提供的" in prompt


def test_build_prompt_without_evidence_backward_compatible():
    prompt = goal_verifier._build_prompt("目标", "标准", "产出")
    assert "【已为你预先收集的工作区证据】" not in prompt
