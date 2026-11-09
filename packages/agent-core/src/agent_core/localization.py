"""前置元数据（frontmatter）本地化辅助函数。

面向用户的定义字段（Agent 的 ``display_name``/``description``/``placeholder``/
``starter_prompts``，技能/工作流的 ``description``）既可以是普通标量——所有
语言共用一个值，即现有文件使用的国际化之前的形态——也可以是语言映射：

    description:
      en-US: "Authoritative English one-liner."
      zh-CN: "一句话中文说明。"

在这里统一两种形态，让每个下游消费方遵循同一契约：为旧版列、面向 LLM 的提示
词和排序键提供单一的**规范（canonical）**字符串，同时为界面序列化保留完整语言
映射。规范语言可由调用方选择，因为两大家族有所不同：

- Agent 界面字段以中文优先，使现有中文内容继续驱动 ``agents.display_name``
  排序键和 ``[display_name]:`` 历史前缀；
- 技能/工作流描述以英文优先，使注入 LLM 提示词（``## Available Workflows``）
  的文本保持字节级一致。
"""
from __future__ import annotations

import re

#: 语言形态的映射键：``zh``、``en-US``——不是任意字典键，因此技能引用
#: （``{slug, description}``）或其他结构化前置元数据值，绝不会被误判为翻译映射。
LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def is_locale_map(value: object) -> bool:
    """当 *value* 是非空字典且所有键都是语言代码时为真。"""
    return isinstance(value, dict) and bool(value) and all(
        isinstance(key, str) and LOCALE_RE.match(key) for key in value
    )


def normalize_i18n_field(
    value: object,
    *,
    canonical_order: tuple[str, ...] = ("zh-CN", "en-US"),
) -> tuple[str | None, dict[str, str] | None]:
    """规范化一个标量或语言映射的前置元数据字段。

    返回 ``(规范值, 语言映射)``：

    - 普通字符串 → ``(值, None)``——所有语言共用一个值；
    - 字符串值的语言映射 → ``(选中值, 完整映射)``——规范条目是映射中存在的
      *canonical_order* 里的第一种语言（都不匹配时退化为映射的第一个条目）；
    - 其他任何东西（None、数字、含非语言键或非字符串值的字典）→
      ``(None, None)``——调用方记录/跳过，绝不抛出，使一个坏文件不会导致整个
      目录同步失败。
    """
    if isinstance(value, str):
        return value, None
    if is_locale_map(value):
        entries = {
            str(key): str(raw)
            for key, raw in value.items()  # type: ignore[union-attr]
            if isinstance(raw, str)
        }
        if not entries:
            return None, None
        for locale in canonical_order:
            if locale in entries:
                return entries[locale], entries
        first = next(iter(entries))
        return entries[first], entries
    return None, None


def normalize_i18n_prompts(
    value: object,
    *,
    canonical_order: tuple[str, ...] = ("zh-CN", "en-US"),
) -> tuple[list[str], dict[str, list[str]] | None]:
    """规范化 ``starter_prompts``：标量、共享列表或语言映射。

    返回 ``(规范列表, 语言映射)``。裸字符串是一个提示词（提示词内含逗号——
    绝不按逗号拆分）；列表由所有语言共用；语言映射为每种语言携带一个列表
    （或单个字符串）。空白条目会丢弃。其他任何形态产生 ``([], None)``。
    """
    if isinstance(value, str):
        text = value.strip()
        return ([text] if text else []), None
    if isinstance(value, (list, tuple)):
        return (
            [item.strip() for item in value if isinstance(item, str) and item.strip()],
            None,
        )
    if is_locale_map(value):
        per_locale: dict[str, list[str]] = {}
        for key, raw in value.items():  # type: ignore[union-attr]
            if isinstance(raw, str):
                text = raw.strip()
                if text:
                    per_locale[str(key)] = [text]
            elif isinstance(raw, (list, tuple)):
                items = [
                    item.strip() for item in raw if isinstance(item, str) and item.strip()
                ]
                if items:
                    per_locale[str(key)] = items
        if not per_locale:
            return [], None
        for locale in canonical_order:
            if locale in per_locale:
                return per_locale[locale], per_locale
        first = next(iter(per_locale))
        return per_locale[first], per_locale
    return [], None
