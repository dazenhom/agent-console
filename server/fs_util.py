"""文件系统工具：路径安全 + 轻量 frontmatter 解析/序列化。

memory_store / agent_store 共用。刻意不引入 PyYAML，手写一个只覆盖本项目实际
用到的 frontmatter 子集（顶层 `key: value` + 一层缩进的 `metadata:` 嵌套 map）的
解析器，保持依赖闭包只有 fastapi/uvicorn。
"""
import re
from pathlib import Path

# 文件名白名单：字母数字 . _ -，不允许任何路径分隔符
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
# frontmatter：开头的 ---\n ... \n--- 块
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


def safe_join(base: Path, name: str) -> Path:
    """把 name 安全地拼到 base 下，防止 ../ 越界。

    双重防御：① 字符白名单拒绝任何分隔符/点路径；② resolve() 后用
    is_relative_to 兜底符号链接和规范化绕过。返回解析后的绝对路径。
    name 不应包含目录部分，可带或不带 .md 扩展名（由调用方决定）。
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("非法文件名")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("文件名仅允许字母、数字、. _ -")
    base_r = base.resolve()
    target = (base_r / name).resolve()
    if not target.is_relative_to(base_r):
        raise ValueError("路径越界")
    return target


def safe_path_under(base: Path, relpath: str) -> Path:
    """把 relpath（可含多级子目录）安全地解析到 base 之下，防 ../ 越界。

    与 safe_join 区别：允许目录分隔符和多级路径（产物预览要读 workdir 下任意层级的文件），
    但 resolve() 后仍用 is_relative_to 兜底，确保不越出 base（含符号链接规范化）。
    relpath 可以是绝对路径（取其相对 base 的部分）或相对路径。
    """
    if not relpath:
        raise ValueError("空路径")
    base_r = base.resolve()
    p = Path(relpath)
    # 绝对路径：必须本来就在 base 下；相对路径：拼到 base 下
    target = p.resolve() if p.is_absolute() else (base_r / p).resolve()
    if not (target == base_r or target.is_relative_to(base_r)):
        raise ValueError("路径越界（只能访问会话工作目录内的文件）")
    return target


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _parse_simple_yaml(block: str) -> dict:
    """解析 frontmatter 子集：顶层标量 + 一层缩进的嵌套 map。

    例：
        name: foo
        description: bar
        metadata:
          type: project
          node_type: memory
    """
    meta: dict = {}
    cur_map: dict | None = None  # 当前正在填充的嵌套 map
    for line in block.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in (" ", "\t")
        if ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        key = key.strip()
        val = val.strip()
        if indented and cur_map is not None:
            cur_map[key] = _unquote(val)
        elif not val:
            # 顶层 key 后无值 → 开一个嵌套 map
            cur_map = {}
            meta[key] = cur_map
        else:
            meta[key] = _unquote(val)
            cur_map = None
    return meta


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆出 (frontmatter dict, body)。无 frontmatter 时返回 ({}, 原文)。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    return _parse_simple_yaml(m.group(1)), m.group(2)


def _needs_quote(v: str) -> bool:
    # 含 YAML 里有歧义的字符时加引号，保证 round-trip 安全
    return bool(v) and (v[0] in "#&*!|>%@`\"'" or ": " in v or v.strip() != v)


def _fmt_val(v) -> str:
    s = "" if v is None else str(v)
    return f'"{s}"' if _needs_quote(s) else s


def dump_frontmatter(meta: dict, body: str) -> str:
    """把 (meta, body) 序列化回带 frontmatter 的文本。

    保留传入 meta 的全部字段（含 tclaude 写入的 originSessionId 等未知字段），
    嵌套 dict 缩进两格。body 前导空行被规整为恰好一个空行。
    """
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for ik, iv in v.items():
                lines.append(f"  {ik}: {_fmt_val(iv)}")
        else:
            lines.append(f"{k}: {_fmt_val(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")
