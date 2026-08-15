# coding: utf-8
"""精选池内稳定行业层级代理的唯一实现。

这里不把全池宽度伪装成板块，也不使用昨日涨停、连板等动态标签。
若最细层级只有一个成员，则逐级回退到细分行业、一级行业；仍不足两个
成员时明确返回不可用。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence

import numpy as np


HIERARCHY = ("niche", "stable_theme", "subindustry", "primary_industry")


def build_group_returns(
    returns: Mapping[str, float],
    taxonomy: Mapping[str, Any],
) -> Dict[str, Dict[str, list]]:
    groups: Dict[str, Dict[str, list]] = {level: defaultdict(list) for level in HIERARCHY}
    symbols = taxonomy.get("symbols", {})
    for symbol, value in returns.items():
        item = symbols.get(symbol, {})
        for level in ("niche", "subindustry", "primary_industry"):
            key = str(item.get(level) or "").strip()
            if key:
                groups[level][key].append(float(value))
        for theme in item.get("stable_themes") or []:
            key = str(theme or "").strip()
            if key:
                groups["stable_theme"][key].append(float(value))
    return groups


def select_sector_context(
    symbol: str,
    groups: Mapping[str, Mapping[str, Sequence[float]]],
    taxonomy: Mapping[str, Any],
) -> Dict[str, Any]:
    item = taxonomy.get("symbols", {}).get(symbol, {})
    levels = []
    niche = str(item.get("niche") or "").strip()
    if niche:
        levels.append(("niche", niche))
    theme_candidates = []
    for position, theme in enumerate(item.get("stable_themes") or []):
        key = str(theme or "").strip()
        values = list(groups.get("stable_theme", {}).get(key, [])) if key else []
        if len(values) >= 2:
            theme_candidates.append((len(values), position, key))
    if theme_candidates:
        _, _, key = min(theme_candidates)
        levels.append(("stable_theme", key))
    # 已有明确稳定主题但池内无同类时，不得再用宽泛行业把不同驱动因素混在一起。
    if not item.get("stable_themes"):
        for level in ("subindustry", "primary_industry"):
            key = str(item.get(level) or "").strip()
            if key:
                levels.append((level, key))

    for level, key in levels:
        values = list(groups.get(level, {}).get(key, []))
        if len(values) < 2:
            continue
        breadth = float(np.mean(np.asarray(values, dtype=float) > 0))
        median_return = float(np.median(values))
        state = 1 if breadth >= 0.55 and median_return > 0 else (-1 if breadth <= 0.40 and median_return < 0 else 0)
        return {
            "state": state,
            "confidence": "HIGH" if len(values) >= 4 else "MEDIUM",
            "level": level,
            "key": key,
            "source": f"SELECTED_POOL_{level.upper()}_PROXY",
            "member_count": len(values),
            "breadth": breadth,
            "median_return_5d": median_return,
        }
    return {
        "state": 0,
        "confidence": "LOW",
        "level": "unavailable",
        "key": str(item.get("niche") or "UNCLASSIFIED"),
        "source": "SECTOR_UNAVAILABLE_SINGLETON",
        "member_count": 0,
        "breadth": None,
        "median_return_5d": None,
    }
