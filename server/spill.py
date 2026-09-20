"""超长文本落盘 + 预览替换（spill）：把塞不进 prompt 的大段证据存成文件，
prompt 里只留首尾预览 + 文件绝对路径，让子进程自己按需 read/grep 捞全文。

为什么需要：编排层拼给一次性子进程的 prompt 里，证据字段全是硬编码盲截断
（goal_verifier 的 produced[:3000]、cmd_result[:2000]、git_diff[:1500] 等）。
被砍掉的部分无声消失，验收员看不到关键尾部就只能判 CONTINUE——而 verifier 的
铁律是"任何不确定一律 CONTINUE"，于是盲截断直接制造假阴性、白烧一轮迭代。

替换成 spill 后语义变了：全文一定在磁盘上，预览带首尾两头，且明确告诉模型
"完整内容在这个路径，用 read/grep 去看"。这些一次性子进程都是带 shell 和 cwd 的
真 agent（codex exec，见 codex_oneshot），有能力读文件，所以定位符不是空话。

不负责决定"哪些字段该 spill"——调用方按字段重要性自己选，本模块只做
"给定预算，要么原样返回，要么落盘+返回替换文本"这一件事。
"""
import hashlib
import re
from pathlib import Path

from . import db

# 落盘目录与 job_logs 并列（同在 DB 所在的网络盘下），避免留本地盘
_SPILL_DIRNAME = "spill"

# 预览首尾配比：头部多给一些（通常是结构/表头/开头的关键信息），尾部留够看结论/报错
_HEAD_RATIO = 0.6

# 文件名里保留的 label 字符，其余一律换成下划线，避免路径穿越和奇怪字符
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _spill_dir() -> Path:
    d = Path(db.config.DB_PATH).parent / _SPILL_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_label(label: str) -> str:
    s = _SAFE_LABEL_RE.sub("_", (label or "text").strip())[:40]
    return s or "text"


def _notice(path: str, omitted: int) -> str:
    """给模型的定位符说明。措辞直接告诉它怎么把全文捞回来，不用猜。"""
    return (f"（此处省略 {omitted} 字节。完整内容已存于文件：{path}"
            f" —— 需要时请直接读取该文件，或用 grep 在其中检索关键字。）")


def spill_text(text: str, budget: int, *, label: str = "text",
               session_id: str | None = None) -> str:
    """把 text 压到 budget 字节以内：够短原样返回，超长则落盘并返回首尾预览+定位符。

    budget 是**返回值**的 UTF-8 字节上限（不是输入上限），也就是这段文本最终占用
    prompt 的字节数。notice 自身的字节开销从 budget 里预扣，因此返回值恒不超过
    budget —— 这是与盲截断最重要的区别：调用方可以据此精确算 prompt 预算。

    budget <= 0 视为不限制，原样返回（让调用方能用一个配置项整体关掉本机制）。

    退化路径（都保证不超预算、且绝不因 spill 反而变长）：
    - 落盘失败：退回纯截断 + 说明落盘失败，不让 IO 故障阻断主流程；
    - budget 小到连 notice 都放不下：返回被截断的 notice；
    - 全文本身不超 budget：原样返回，不产生垃圾文件。
    """
    if not text:
        return text
    if budget is None or budget <= 0:
        return text

    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text

    # 先落盘全文，再据实际路径算 notice 长度（路径长度影响预算，必须先拿到真路径）。
    # 建目录也在 try 内：磁盘/权限故障绝不能抛给调用方中断验收流程。
    try:
        d = _spill_dir()
        digest = hashlib.sha1(raw).hexdigest()[:12]
        prefix = f"{session_id}-" if session_id else ""
        path = d / f"{prefix}{_safe_label(label)}-{digest}.txt"
        # 已存在同内容文件（同一段证据反复入 prompt）直接复用，不重复写盘
        if not path.exists():
            path.write_text(text, encoding="utf-8")
        located = str(path)
    except Exception as e:
        # 落盘失败不能拖垮主流程：退回截断，但明确告知模型内容不完整且为何不完整
        fallback_note = f"（此处省略 {len(raw)} 字节，且落盘失败：{type(e).__name__}，无法提供完整内容路径。）"
        return _truncate_with_note(text, budget, fallback_note)

    return _truncate_with_note(text, budget, _notice(located, 0), full_bytes=len(raw))


def _truncate_with_note(text: str, budget: int, note: str,
                        full_bytes: int | None = None) -> str:
    """按 budget 组装 "首部预览 + 空行 + note + 空行 + 尾部预览"，整体不超 budget 字节。

    note 的字节开销先从 budget 扣掉，剩下的才分给首尾预览；note 里的省略字节数按
    实际省略量回填（先算长度再回填会变长，故用固定宽度占位再替换，见下）。
    """
    raw = text.encode("utf-8")

    # note 里的数字会随预览长度变化，而 note 长度又决定预览长度 —— 互相依赖。
    # 用"最坏情况长度"（省略量不可能超过全文字节数）预留，避免迭代求解：
    # 以全文字节数的位数为准，多算几字节永远安全，绝不会算少而超预算。
    if full_bytes is not None:
        note_probe = _notice_probe(note, full_bytes)
    else:
        note_probe = note
    reserve = len(note_probe.encode("utf-8")) + 2  # +2 是 note 前后各一个换行

    if reserve >= budget:
        # 预算连 note 都放不下：只回 note 本身，按字节截断到预算内。
        # 此时无预览可给，但至少保住"内容在哪"这个信息（或其前半段）。
        return _cut_bytes(note_probe, budget)

    avail = budget - reserve
    head_bytes = int(avail * _HEAD_RATIO)
    tail_bytes = avail - head_bytes
    head = _cut_bytes(text, head_bytes)
    tail = _cut_bytes_from_end(text, tail_bytes)

    omitted = len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    if omitted <= 0:
        # 首尾预览已经覆盖全文（预算接近全长时可能发生）：直接给全文，别塞无意义的 note
        return text
    final_note = _notice_probe(note, omitted) if full_bytes is not None else note
    return f"{head}\n{final_note}\n{tail}"


def _notice_probe(note: str, omitted: int) -> str:
    """把 notice 模板里的省略字节数替换成 omitted。

    note 由 _notice() 生成，形如"（此处省略 N 字节。…）"，这里只替换那个 N。
    """
    return re.sub(r"省略 \d+ 字节", f"省略 {omitted} 字节", note, count=1)


def _cut_bytes(s: str, limit: int) -> str:
    """按 UTF-8 字节数截取前 limit 字节，不切坏多字节字符。"""
    if limit <= 0:
        return ""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore")


def _cut_bytes_from_end(s: str, limit: int) -> str:
    """按 UTF-8 字节数截取末 limit 字节，不切坏多字节字符。"""
    if limit <= 0:
        return ""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[-limit:].decode("utf-8", errors="ignore")
