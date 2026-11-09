"""把 issue 正文从 Markdown 转换为 Jira wiki 标记。

Jira 的 REST v2 API 把字符串 ``body`` 渲染为 Jira wiki 标记而非 Markdown。因此
Markdown 的 ``### 标题`` 会回来变成三级嵌套编号列表（"1.1.1 标题"），``-``
项目符号 / ``1.`` 列表渲染为纯文本。每个写入 Jira 的正文都经过
``markdown_to_wiki``，使智能体可以继续写 Markdown，渲染的注释保持可读。

转换刻意基于行、务实，不是完整 Markdown 解析器。已是 wiki 标记的行（如 Jira 约定
里 ``{color:red}...{color}`` 的自适应数据库标记）原样通过。
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_BLOCKQUOTE = re.compile(r"^(?:>+\s?)(.*)$")
_TASKLIST = re.compile(r"^(\s*)[-*+]\s+\[[ xX]\]\s*(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]!][^\]]*)\]\(([^)\s]+)\)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
# 已是 wiki 的行不得被重新解释为 Markdown。
_WIKI_PREFIX = re.compile(r"^(?:h[1-6]\.|bq\.|----|\{)")


def _inline(md: str) -> str:
    """转换行内 Markdown 构造（图片、链接、代码、加粗）。"""
    s = _IMAGE.sub(r"!\2|\1!", md)
    s = _LINK.sub(r"[\1|\2]", s)
    s = _CODE_SPAN.sub(r"{{\1}}", s)
    s = _BOLD.sub(r"*\1*", s)
    return s


def markdown_to_wiki(md: str) -> str:
    if not md:
        return md
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    # 当前列表块的（缩进, 深度）栈——相对深度同时处理 2 空格和 4 空格的
    # Markdown 嵌套。出现非列表行时重置。
    indent_stack: list[tuple[int, int]] = []

    def list_item_depth(indent: int) -> int:
        while indent_stack and indent < indent_stack[-1][0]:
            indent_stack.pop()
        if indent_stack and indent == indent_stack[-1][0]:
            return indent_stack[-1][1]
        depth = indent_stack[-1][1] + 1 if indent_stack else 1
        indent_stack.append((indent, depth))
        return depth

    def reset_list() -> None:
        indent_stack.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 围栏代码块 → {code} 块。
        fence = _FENCE.match(line)
        if fence:
            if in_code:
                out.append("{code}")
                in_code = False
            else:
                lang = fence.group(1)
                out.append("{code" + (f":{lang}" if lang else "") + "}")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(line)
            i += 1
            continue

        # 已是 wiki 标记（如 {color:red}[SELF-ADAPT-DB] ... {color}）：原样通过，
        # 而非重新解释为 Markdown。
        if _WIKI_PREFIX.match(stripped):
            out.append(line)
            reset_list()
            i += 1
            continue

        # Markdown 表格 → wiki 表格（||表头|| / |单元格|）。分隔行（|---|---|）
        # 没有 wiki 对应物，被丢弃。
        if _TABLE_SEP.match(line) or _TABLE_ROW.match(line):
            block: list[str] = []
            has_sep = False
            while i < len(lines) and (_TABLE_SEP.match(lines[i]) or _TABLE_ROW.match(lines[i])):
                has_sep = has_sep or bool(_TABLE_SEP.match(lines[i]))
                block.append(lines[i])
                i += 1
            rows = [
                [c.strip() for c in raw.strip().strip("|").split("|")]
                for raw in block if not _TABLE_SEP.match(raw)
            ]
            if rows:
                if has_sep:
                    header, body = rows[0], rows[1:]
                    out.append("||" + "||".join(_inline(c) for c in header) + "||")
                else:
                    body = rows
                for row in body:
                    out.append("|" + "|".join(_inline(c) for c in row) + "|")
            reset_list()
            continue

        # 水平线。
        if _HR.match(line):
            out.append("----")
            reset_list()
            i += 1
            continue

        # 引用块 → bq.
        bq = _BLOCKQUOTE.match(line)
        if bq:
            out.append("bq. " + _inline(bq.group(1)))
            reset_list()
            i += 1
            continue

        # ATX 标题 → h1. ... h6. 否则裸的 Markdown "###" 会渲染为三级嵌套编号
        # 列表（"1.1.1"）。
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"h{level}. " + _inline(heading.group(2)))
            reset_list()
            i += 1
            continue

        # 列表项：wiki 按深度级别重复标记来嵌套。
        task = _TASKLIST.match(line)
        if task:
            out.append("*" * list_item_depth(len(task.group(1))) + " " + _inline(task.group(2)))
            i += 1
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append("*" * list_item_depth(len(bullet.group(1))) + " " + _inline(bullet.group(2)))
            i += 1
            continue
        ordered = _ORDERED.match(line)
        if ordered:
            out.append("#" * list_item_depth(len(ordered.group(1))) + " " + _inline(ordered.group(2)))
            i += 1
            continue

        # 普通段落。
        out.append(_inline(line))
        reset_list()
        i += 1

    return "\n".join(out)
