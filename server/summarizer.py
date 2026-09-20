"""行摘要 + 标题：给会话列表生成「这条会话在干什么、进展到哪」。

主路径用便宜档 codex 模型（CHEAP_MODEL）一次性调用同时产出 title + summary（原来行摘要
与起标题是两次独立调用，现在合并为一次、prompt 要求严格两行输出）；失败/超时回退到
启发式截断。这是**独立的一次性子进程**（不是会话的常驻进程），绝不碰会话上下文。
"""
import re

from . import config, db
from .codex_oneshot import run_codex_oneshot_text


def _heuristic(user_text: str, reply_text: str, limit: int = 40) -> str:
    """启发式兜底：按句切分取最后一个长度 ≥8 的完整句（结论通常在结尾），没有合格
    句时退回复述前 N 字。优先看 Agent 回复，没有则取用户指令。"""
    src = (reply_text or "").strip() or (user_text or "").strip()
    if not src:
        return ""
    sents = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"[。！？\n]", src)]
    for sent in reversed(sents):
        if len(sent) >= 8:
            return sent[:limit] + ("…" if len(sent) > limit else "")
    s = re.sub(r"\s+", " ", src)
    return s[:limit] + ("…" if len(s) > limit else "")


# 合并调用的说明段：严格两行输出（TITLE：/SUMMARY：），辨识度硬规则（具体对象、带数字、
# 禁泛词、TITLE 管整条会话主线 / SUMMARY 管最近一轮）全部写死在 prompt 里。
_PROMPT_HEAD = (
    "你在给一个工程师的 AI 会话列表生成条目文案，目的是让他扫一眼就知道「这条会话在干什么、进展到哪」。\n\n"
    "输出严格两行，不要任何前后缀、引号、编号、markdown：\n"
    "第1行以 TITLE： 开头，不超过 20 个汉字，格式「对象 + 在做什么」。对象必须是具体的项目/模块/文件/数据集名，"
    "直接抄原文里的专有名词（如 job_store、英文AST线、v17、WER）。\n"
    "第2行以 SUMMARY： 开头，不超过 30 个汉字，格式「动作 + 对象 + 结果/数字」。必须写出这一轮的结论或数字，"
    "没有结论就写当前卡在哪。\n\n"
    "硬规则：\n"
    "- 禁止出现泛词：完成了任务、进行了处理、已处理、相关工作、若干、一些、进行分析。\n"
    "- 有数字（行数/百分比/耗时/版本号）必须带上。\n"
    "- 只描述事实，不评价。\n"
    "- TITLE 描述整条会话的主线任务，SUMMARY 只描述最近这一轮的进展，两行不要重复。\n\n"
    "示例：\n"
    "TITLE：job_store 超时根因定位\n"
    "SUMMARY：定位到子进程未指定 stdin，加 DEVNULL 后 8/8 通过\n\n"
    "TITLE：英文 AST 线产量瓶颈分析\n"
    "SUMMARY：瓶颈是召回词表仅 8 个词，放宽到 33 个后召回 3.71 倍\n\n"
    "TITLE：stage2 数据配比核对\n"
    "SUMMARY：核对 v17 各 source 比例，粤语权重偏高已降至 0.6\n"
)

# 有原标题时追加的判据段：点破"当前标题可能只是机械截断残句"。不点破的话模型会把
# 初始标题当权威一路 KEEP 到底（旧版 73% 的标题被钉死在截断残句上，根因就在这）。
_PROMPT_TITLE_PARA = (
    "\n当前标题是『{current_title}』。注意它可能只是把用户第一句话机械截断的残句"
    "（特征：以 … 结尾、以「帮我/阅读/读取」开头、含裸路径、读不出在干什么）——"
    "这种情况必须重写成合格 TITLE，不要保留。\n"
    "只有当它已经是一个合格的、准确概括整条会话的 TITLE 时，第1行才原样输出 KEEP。\n"
)


def _strip_garbled(s: str) -> str:
    """剥掉模型偶发吐在结尾的孤立非中文非 ASCII 乱码尾巴（如西里尔字母 ропа）。
    末尾的纯拉丁尾巴（如 bic）发生率低（约 2/40），为剥它引入规则会误伤 GPU/PR/stdin
    这类正常英文标识符，得不偿失，故允许保留、不再剥。"""
    s = re.sub(r"[^\x00-\x7f一-鿿…]+$", "", s)
    return s.strip()


def _parse_merged_output(text: str) -> tuple[str, str]:
    """解析合并调用的两行输出 → (title, summary)。按行找 TITLE：/SUMMARY： 前缀（全角、
    半角冒号都认），title 限 24 字、summary 限 40 字。title 为空串 = 保留原标题（含模型
    回 KEEP 与整行缺失两种情况）；只出其一时另一个照常返回，不整体丢弃。"""
    title = summary = ""
    for line in text.splitlines():
        line = line.strip()
        if not title:
            m = re.match(r"^TITLE[：:]\s*(.*)$", line)
            if m:
                t = m.group(1).strip().strip('"“”')
                # KEEP 判定沿用旧去噪正则：剥常见引号/结尾标点再比对（仅用于判断）。
                # 必须先于乱码清洗做，保留这个既有顺序（之前修过，不能回退）。
                if re.sub(r"[\"'『』「」。.,!！]", "", t).strip().upper() == "KEEP":
                    title = ""
                    continue
                title = _strip_garbled(t)[:24]
                continue
        if not summary:
            m = re.match(r"^SUMMARY[：:]\s*(.*)$", line)
            if m:
                summary = _strip_garbled(m.group(1).strip().strip('"“”'))[:40]
    return title, summary


async def summarize_and_title(convo: str, current_title: str = "",
                              user_text: str = "", reply_text: str = "") -> tuple[str, str]:
    """一次调用同时生成 title + summary（合并原先行摘要与起标题两次调用）。

    convo 是会话摘录（session_hub._build_title_convo 的产物，含 skill 前言剥离与去重），
    user_text/reply_text 是刚结束这一轮的全文（SUMMARY 的主要素材）。返回 (title, summary)：
    title 为空串 = 保留原标题；两者独立容错（只出其一时另一个照常返回）。AI 失败/关闭时
    title 为空串、summary 回退启发式。永不抛异常。"""
    if not config.SUMMARY_ENABLED:
        return "", _heuristic(user_text, reply_text)
    user_clean = re.sub(r"\s+", " ", (user_text or "").strip())[:300]
    reply_clean = re.sub(r"\s+", " ", (reply_text or "").strip())[:800]
    convo_clean = re.sub(r"\s+", " ", (convo or "").strip())[:800]
    if not (user_clean or reply_clean or convo_clean):
        return "", ""
    parts = [_PROMPT_HEAD]
    if current_title:
        parts.append(_PROMPT_TITLE_PARA.format(current_title=current_title))
    body = []
    if user_clean or reply_clean:
        body.append(f"最近一轮：用户说『{user_clean}』，助手回复『{reply_clean}』")
    if convo_clean:
        body.append(f"会话摘录（首条 + 最近几轮）：『{convo_clean}』")
    parts.append("对话内容：\n" + "\n".join(body))
    try:
        jid, text, _, status = await run_codex_oneshot_text(
            "line_summary", "\n".join(parts), config.SUMMARY_TIMEOUT,
            model=config.CHEAP_MODEL,
        )
    except Exception:
        return "", _heuristic(user_text, reply_text)
    if status != "success":
        return "", _heuristic(user_text, reply_text)
    # 排障可观测性：把模型原始返回写进 job_runs.output（照 kanban 的写法），解析出问题
    # 时能直接看到模型到底吐了什么。
    try:
        db.set_job_output(jid, text)
    except Exception:
        pass
    title, summary = _parse_merged_output(text)
    if not summary:
        summary = _heuristic(user_text, reply_text)
    if not config.TITLE_REFRESH_ENABLED:
        title = ""  # 手动关掉标题自动刷新时只出 summary，不覆盖标题
    return title, summary


async def summarize(user_text: str, reply_text: str) -> str:
    """兼容入口：只要行摘要（内部走合并调用，无会话摘录时只看本轮对话）。永不抛异常。"""
    try:
        _, summary = await summarize_and_title("", "", user_text, reply_text)
        return summary
    except Exception:
        return _heuristic(user_text, reply_text)


async def gen_title(convo: str, current_title: str = "") -> str:
    """兼容入口：只要标题（内部走合并调用）。返回空串 = 保留原标题。永不抛异常。

    current_title 非空时交给模型判断是否需要换标题（残句必须重写、准确才 KEEP）。"""
    try:
        title, _ = await summarize_and_title(convo, current_title)
        return title
    except Exception:
        return ""
