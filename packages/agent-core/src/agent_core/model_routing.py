"""模型路由辅助函数，用于自动选择模型。

本模块是纯函数集合，同时供 API 处理器（逐轮预分类路由）与 agent-core
（规划子任务路由）使用。复杂度层级分为 low/mid/high；当目标层级缺失时，
回退到最近的可用层级，优先选择更便宜的。
"""
from __future__ import annotations

TIERS = ("low", "mid", "high")

# 最近层级回退链：目标层级缺失时，先找更便宜的，再找更贵的。
_FALLBACK = {
    "low": ("low", "mid", "high"),
    "mid": ("mid", "low", "high"),
    "high": ("high", "mid", "low"),
}
# 未知/缺失复杂度：优先 mid，再找更便宜的，最后更贵的。
_DEFAULT_CHAIN = ("mid", "low", "high")


def resolve_tier_config(tier_map: dict[str, str], complexity: str | None) -> str | None:
    """返回服务于指定复杂度层级的 config_id。

    精确层级匹配优先；否则沿最近层级回退链查找（优先更便宜的）。
    ``complexity`` 不在 low/mid/high 内（含 None）时，通过默认链解析。
    ``tier_map`` 为空时返回 None。
    """
    if not tier_map:
        return None
    # complexity 可能来自未校验的计划 JSON；若为 list/dict，dict 查找会抛出
    # TypeError，而不是正常回退。
    key = complexity if isinstance(complexity, str) else ""
    chain = _FALLBACK.get(key, _DEFAULT_CHAIN)
    for tier in chain:
        config_id = tier_map.get(tier)
        if config_id:
            return config_id
    return None


def cheapest_tier(tier_map: dict[str, str]) -> str | None:
    """返回已配置的最便宜层级名称（low→mid→high）。

    用于在自动模式下挑选预分类模型。映射为空时返回 None。
    """
    for tier in TIERS:
        if tier in tier_map:
            return tier
    return None
