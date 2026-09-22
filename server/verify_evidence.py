"""确定性证据包：验收前用纯代码把评委本要自己探的东西先收集好，减少自探命令。

为什么需要（实测数据）：goal_verify 占 oneshot 成本 96.5%，input ≈ 19768*T + 1313*T²
（T = 命令数 + 1），corr(命令数, input) = 0.806 —— 评委每条命令都把整个上下文重放一遍，
命令数是唯一有效杠杆。而评委 317 条命令里 read_file/grep/list_tree/git_status_diff/stat
占 64.4%，全都能被确定性代码替代；66/114 个 job 的"真需执行"命令数中位数是 0。
所以这里先替评委做掉无争议的探查：工作区文件树、本轮改动文件摘要、完成标准关键词命中行、
git 历史。注意只塞证据不加引导语**不会**让评委少探（已有数据证明），引导语在
goal_verifier 的 prompt 里与证据同批上线。

性能红线：绝不阻塞 scheduler 的 event loop。整体在线程里跑（asyncio.to_thread），
内部带 wall-clock 5s 预算：每步之间查预算，子进程直接用剩余预算当 timeout，超时返回
已收集部分。任何异常一律降级为空证据，绝不影响验收主流程（宁可没证据，也不能卡住或
误判）。子进程只在两个明确用途上起（git 摘要 / 关键词检索，各一次），绝不按文件或按
关键词循环 spawn。
"""
import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, spill, worktree
from .logging_util import get_logger

log = get_logger(__name__)

BUDGET_SEC = 5.0          # 整体 wall-clock 预算（含子进程），超时返回已收集部分
TREE_MAX_FILES = 60       # 文件树只留 mtime 最新 N 个
TREE_MAX_DEPTH = 3        # 相对 workdir 的目录深度上限
TREE_WALK_MAX = 20000     # 遍历条目数上限（防病态大目录把 5s 全吃掉）
PREVIEW_BYTES = 1500      # 改动文件首尾各取多少字节
PREVIEW_MAX_FILES = 5     # 最多预览几个改动文件（总量再由 spill 硬兜底）
KEYWORD_MAX = 12          # 从完成标准里最多抽多少个关键词
KEYWORD_PER_WORD = 5      # 每个关键词最多留几行命中
KEYWORD_MAX_LINES = 200   # 命中行总上限
CMD_OUT_MAX_CHARS = 20000 # 单条 git 命令输出最多留多少字符（超出必然被 spill 砍掉）

# 遍历时跳过的目录：版本库元数据/依赖/缓存/构建产物，评委看它们没有意义
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "graphify-out"}
# 取首尾预览的"报告类"后缀：结论/清单基本都在这些文件里，代码文件只列路径不贴正文
PREVIEW_EXTS = {".md", ".markdown", ".rst", ".txt", ".json", ".jsonl", ".csv",
                ".yaml", ".yml", ".html"}
PREVIEW_NAME_HINTS = ("report", "summary", "报告", "结论", "汇总")

_TICK_RE = re.compile(r"`([^`\n]+)`")
# 路径：含 / 的 token，或带常见源码/数据后缀的文件名
_PATH_RE = re.compile(
    r"[A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+"
    r"|[A-Za-z0-9_-]+\.(?:py|js|ts|md|json|jsonl|txt|sh|yml|yaml|sql|html|css|csv|toml|ini)\b")
# 标识符：snake_case / camelCase / PascalCase / SCREAMING_SNAKE（排除普通英文单词，
# 否则关键词命中全是噪声；全大写常量在完成标准里很常见，单列一支）
_IDENT_RE = re.compile(
    r"\b(?:[a-z][a-z0-9]*_[a-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9]*"
    r"|[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_STRIP_CHARS = "，。；;,)）:：'\"`*"


async def collect(workdir: str, stop_condition: str, base_ref: str = "") -> str:
    """收集一份确定性证据包（工作区文件树 + 本轮改动文件摘要 + 完成标准关键词命中行 +
    git 历史），总字节数受 config.GOAL_SPILL_EVIDENCE_BYTES 硬约束（超限落盘 + 首尾预览）。

    返回 ""表示无证据可用（目录不存在/收集异常/全空）：调用方按"没有证据"降级，
    绝不因此改变验收流程。本函数是唯一入口，永不抛异常给调用方。"""
    try:
        # 线程里跑：os.walk 与 subprocess 放到 event loop 上会卡住 scheduler 的 tick
        return await asyncio.wait_for(
            asyncio.to_thread(_collect_sync, workdir, stop_condition, base_ref),
            timeout=BUDGET_SEC + 2,  # 线程不可取消，这层只是保证 await 侧一定有界
        )
    except Exception as e:
        log.warning("验收证据收集失败，降级为无证据：%s", e)
        return ""


def _collect_sync(workdir: str, stop_condition: str, base_ref: str) -> str:
    wd = Path(workdir or "")
    if not workdir or not wd.is_dir():
        return ""
    deadline = time.monotonic() + BUDGET_SEC
    sections: list[str] = []

    tree = _file_tree(wd, deadline)
    if tree:
        sections.append(f"【工作区文件树（mtime 最新 {TREE_MAX_FILES} 个，深度≤{TREE_MAX_DEPTH}）】\n"
                        + "\n".join(tree))

    # 本轮改动走 base..HEAD（base 是 worktree 创建时落库的 HEAD，见 worktree.create）：
    # 没有基线就跳过这一项，绝不猜"哪个 commit 是本轮的"。
    base_ok = worktree.is_commit_sha(base_ref)
    changed = _git_lines(wd, ["diff", "--name-status", f"{base_ref}..HEAD"], deadline) if base_ok else []
    if base_ok:
        preview = _change_previews(wd, changed, deadline)
        if preview:
            sections.append(f"【本轮改动文件摘要（{base_ref[:10]}..HEAD，报告类取首尾各 {PREVIEW_BYTES} 字节）】\n"
                            + "\n\n".join(preview))

    hits = _keyword_hits(wd, stop_condition, deadline)
    if hits:
        sections.append("【完成标准关键词命中行】\n" + "\n".join(hits))

    git_part = _git_summary(wd, base_ref, base_ok, changed, deadline)
    if git_part:
        sections.append(git_part)

    text = "\n\n".join(sections).strip()
    if not text:
        return ""
    # 总预算硬上限：超了落盘 + 返回首尾预览 + 文件路径（评委是带 shell 的 agent，能自己捞全文）
    return spill.spill_text(text, config.GOAL_SPILL_EVIDENCE_BYTES, label="evidence")


def _run(cmd: list[str], cwd: Path, deadline: float) -> str:
    """跑一条只读命令，输出按字符上限截断。失败/超时/命令不存在一律返回空串。"""
    left = deadline - time.monotonic()
    if left <= 0:
        return ""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           errors="replace", timeout=left)
    except Exception:
        return ""
    return (r.stdout or "")[:CMD_OUT_MAX_CHARS].strip()


def _file_tree(wd: Path, deadline: float) -> list[str]:
    """workdir 下 mtime 最新的若干文件（深度≤3），带 size 与 mtime。
    纯 os.walk，不 spawn；边走边查预算，大目录走不完就返回已收集的部分。"""
    found: list[tuple[float, str, int]] = []
    visited = 0
    for root, dirs, files in os.walk(wd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel_root = Path(root).relative_to(wd)
        depth = 0 if str(rel_root) == "." else len(rel_root.parts)
        if depth >= TREE_MAX_DEPTH:
            dirs[:] = []  # 到深度上限就不再往下走
        if visited >= TREE_WALK_MAX or time.monotonic() > deadline:
            break
        prefix = "" if str(rel_root) == "." else str(rel_root) + "/"
        for f in files:
            visited += 1
            try:
                st = (wd / rel_root / f).stat()
            except OSError:
                continue
            found.append((st.st_mtime, prefix + f, st.st_size))
    found.sort(reverse=True)  # mtime 降序；同秒按路径稳定排序
    return [f"{rel}  {size}B  {time.strftime('%m-%d %H:%M', time.localtime(mt))}"
            for mt, rel, size in found[:TREE_MAX_FILES]]


def _git_summary(wd: Path, base_ref: str, base_ok: bool, changed: list[str],
                 deadline: float) -> str:
    """git 历史摘要：有基线时给 base..HEAD 的提交列表 + 改动文件名，无基线时给最近 10 条提交。
    只看历史与文件清单，不给 patch 正文（正文交给 P0 的 git_diff 段与 spill）。"""
    if base_ok:
        log_txt = _run(["git", "log", "--oneline", f"{base_ref}..HEAD"], wd, deadline)
        files = "\n".join(changed) if changed else "(无改动文件)"
        return (f"【本轮提交与改动文件（{base_ref[:10]}..HEAD）】\n"
                f"{log_txt or '(本轮无新提交)'}\n--- 改动文件（状态+路径）---\n{files}")
    log_txt = _run(["git", "log", "--oneline", "-n", "10"], wd, deadline)
    return f"【最近提交】\n{log_txt}" if log_txt else ""


def _git_lines(wd: Path, args: list[str], deadline: float) -> list[str]:
    out = _run(["git", *args], wd, deadline)
    return [ln for ln in out.splitlines() if ln.strip()]


def _want_preview(path: str) -> bool:
    p = Path(path)
    if p.suffix.lower() in PREVIEW_EXTS:
        return True
    name = p.name.lower()
    return any(h in name for h in PREVIEW_NAME_HINTS)


def _change_previews(wd: Path, changed: list[str], deadline: float) -> list[str]:
    """改动文件里报告类文件取首尾各 PREVIEW_BYTES 字节（只读两端，不整文件读入）。"""
    out: list[str] = []
    for line in changed:
        if len(out) >= PREVIEW_MAX_FILES or time.monotonic() > deadline:
            break
        fields = line.split("\t")
        path = fields[-1].strip() if len(fields) > 1 else ""
        status = fields[0].strip() if fields else ""
        if not path or not _want_preview(path):
            continue
        try:
            size = (wd / path).stat().st_size
            with (wd / path).open("rb") as f:
                head = f.read(PREVIEW_BYTES)
                tail = b""
                if size > 2 * PREVIEW_BYTES:
                    f.seek(-PREVIEW_BYTES, os.SEEK_END)
                    tail = f.read(PREVIEW_BYTES)
        except OSError:
            continue
        head_s = head.decode("utf-8", errors="replace").rstrip()
        tail_s = tail.decode("utf-8", errors="replace").rstrip()
        if tail_s:
            out.append(f"== {status} {path}（{size}B，中段省略）==\n{head_s}\n…\n{tail_s}")
        else:
            out.append(f"== {status} {path}（{size}B）==\n{head_s}")
    return out


def _keywords(stop_condition: str) -> list[str]:
    """从完成标准里抽可检索的关键词：反引号内容 / 路径 / 驼峰或下划线标识符。
    抽不出任何东西时返回空列表（调用方据此整段跳过，不做无意义的全文 g/re/p）。

    路径额外补一支 basename（"server/scheduler.py" → "scheduler.py"）：完整路径几乎
    只会以"被引用"的形式出现在别处（import/文档），basename 才是能命中的检索词。"""
    text = stop_condition or ""
    cands: list[str] = []
    for m in _TICK_RE.finditer(text):
        cands.append(m.group(1))
    paths = _PATH_RE.findall(text)
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if "/" in p and len(base) >= 8 and "." in base:
            cands.append(base)
    cands += paths
    cands += _IDENT_RE.findall(text)
    out: list[str] = []
    for c in cands:
        w = (c or "").strip().strip(_STRIP_CHARS)
        # 太短会命中一切、太长必然是噪声；两者都不值得进检索
        if len(w) < 3 or len(w) > 60:
            continue
        if w not in out:
            out.append(w)
        if len(out) >= KEYWORD_MAX:
            break
    return out


def _keyword_hits(wd: Path, stop_condition: str, deadline: float) -> list[str]:
    """在 workdir 里检索完成标准关键词，返回 "路径:行号: 内容" 形式的命中行。
    只起一个检索进程（所有关键词用 -e 一次性传入），绝不按关键词循环 spawn。"""
    words = _keywords(stop_condition)
    if not words:
        return []
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--max-count", str(KEYWORD_PER_WORD),
               *[a for w in words for a in ("-e", w)], "."]
    else:
        cmd = ["grep", "-rn", "-m", str(KEYWORD_PER_WORD), "--exclude-dir=.git",
               *[a for w in words for a in ("-e", w)], "."]
    raw = _run(cmd, wd, deadline)
    if not raw:
        return []
    per_word: dict[str, int] = {}
    out: list[str] = []
    for line in raw.splitlines():
        if len(out) >= KEYWORD_MAX_LINES:
            break
        # 每词最多 5 行：只看全局前 N 行会被"第一个词命中几百行"淹没，把后面的词全挤掉
        hit = next((w for w in words if w in line), None)
        if not hit:
            continue
        if per_word.get(hit, 0) >= KEYWORD_PER_WORD:
            continue
        per_word[hit] = per_word.get(hit, 0) + 1
        out.append(line.strip())
    return out
