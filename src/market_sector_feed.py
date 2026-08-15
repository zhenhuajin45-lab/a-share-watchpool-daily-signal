# coding: utf-8
"""全市场动态板块健康雷达。

板块强弱不等同于涨跌幅。本模块覆盖全市场行业与概念板块，并综合：
- 横截面相对强弱、上涨广度、成交额/换手率/量比、主力净流入；
- 领涨股强度和集中度；
- 涨停/连板梯队（全市场涨停池 + 个股行业/概念映射）；
- 盘中快周期 KDJ(8,2,2)、MACD(5,10,5) 和排名持续性；
- 强势板块的轮入、持续、瞬时脉冲和轮出状态。

精选池内部梯队不在本模块中冒充市场板块；它只能在上层作为旁证。
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


HOSTS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get?",
    "https://82.push2.eastmoney.com/api/qt/clist/get?",
    "https://push2.eastmoney.com/api/qt/clist/get?",
)
LIMIT_UP_URL = "https://push2ex.eastmoney.com/getTopicZTPool?"
BOARD_SCOPES = {
    "INDUSTRY": "m:90+t:2",
    "CONCEPT": "m:90+t:3",
}

# 只排除动态榜单、风格、指数、地域等伪板块；真实产业概念仍参与全市场比较。
EXCLUSION_PATTERNS = (
    "昨日", "连板", "涨停", "首板", "打板", "高振幅", "高换手", "近期新高", "百日新高",
    "融资融券", "沪股通", "深股通", "基金重仓", "机构重仓", "破净", "破增发价", "转债标的",
    "MSCI", "中证", "上证", "深成", "标普", "富时", "创业板综", "创业成份", "AH股",
    "北京板块", "上海板块", "广东板块", "江苏板块", "浙江板块", "山东板块", "四川板块",
    "安徽板块", "福建板块", "湖北板块", "湖南板块", "河南板块", "河北板块", "辽宁板块",
    "吉林板块", "黑龙江板块", "江西板块", "云南板块", "贵州板块", "广西板块", "新疆板块",
    "西藏板块", "海南板块", "陕西板块", "山西板块", "甘肃板块", "青海板块", "宁夏板块",
    "内蒙古板块", "天津板块", "重庆板块",
)

THEME_ALIASES = {
    "医疗研发外包": ("医疗研发外包", "CRO"),
    "创新药服务": ("创新药", "CRO", "医疗研发外包"),
    "合成生物服务": ("合成生物",),
    "锡": ("锡", "小金属"), "锑": ("锑", "小金属"), "铟": ("铟", "小金属"),
    "稀土": ("稀土",), "稀土永磁产业链": ("稀土", "稀土永磁"),
    "铜": ("铜",), "黄金": ("黄金", "贵金属"), "贵金属": ("贵金属", "黄金"),
    "逆变器": ("逆变器", "光伏设备"), "储能": ("储能",),
    "锂电池材料": ("锂电池", "电池化学品"),
    "风电零部件": ("风电设备", "海上风电"), "海上风电": ("海上风电", "风电设备"),
    "智能电网": ("智能电网",), "磷化工": ("磷化工",), "磷肥": ("磷化工", "化肥"),
    "氮肥": ("化肥",), "化肥": ("化肥",), "农药中间体": ("农药",),
    "精细化工": ("化学制品",), "船舶制造": ("船舶制造",),
    "海洋工程": ("海工装备", "船舶制造"), "中药": ("中药",), "中成药": ("中药",),
    "射频器件": ("射频器件", "消费电子"), "消费电子": ("消费电子",),
    "卫星通信产业链": ("卫星通信", "6G概念"),
    "光刻胶": ("光刻胶",), "半导体材料": ("半导体材料",), "橡胶化学品": ("橡胶制品", "化学制品"),
    "跨境电商": ("跨境电商",), "数字供应链": ("跨境电商", "互联网电商"),
    "CMP材料": ("半导体材料",), "湿电子化学品": ("电子化学品", "半导体材料"),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _percentile(value: float, ordered: Sequence[float]) -> float:
    if not ordered:
        return 0.5
    less = sum(item < value for item in ordered)
    equal = sum(item == value for item in ordered)
    return (less + 0.5 * equal) / len(ordered)


def _split_themes(value: Any) -> List[str]:
    text = str(value or "")
    for separator in (",", "，", ";", "；", "/", "、"):
        text = text.replace(separator, "|")
    return [part.strip() for part in text.split("|") if part.strip()]


def _ema(values: Sequence[float], span: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


class FullMarketSectorRadar:
    """异步调用友好的全市场板块事实、健康度与轮动状态适配器。"""

    def __init__(
        self,
        timeout: float = 8.0,
        page_size: int = 100,
        max_pages: int = 20,
        workers: int = 10,
        stock_cache_seconds: int = 240,
    ):
        self.timeout = timeout
        self.page_size = min(max(int(page_size), 1), 100)
        self.max_pages = max_pages
        self.workers = max(1, workers)
        self.stock_cache_seconds = max(60, stock_cache_seconds)
        self.latest: Dict[str, Any] = {"status": "UNINITIALIZED", "rows": [], "by_name": {}}
        self.history: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=120))
        self.stock_cache: Dict[str, Dict[str, Any]] = {}
        self.stock_cache_at: Optional[datetime] = None
        self.limit_up_cache: List[Dict[str, Any]] = []

    def _http_json(self, url: str) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for host in ("",) if url.startswith("http") else HOSTS:
            try:
                request = urllib.request.Request(
                    url if url.startswith("http") else host + url,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/center/"},
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"{type(last_error).__name__}: {str(last_error)[:180]}")

    def _fetch_clist_page(self, page: int, fs: str, fields: str) -> Tuple[List[Dict[str, Any]], int]:
        query = urllib.parse.urlencode({
            "pn": page, "pz": self.page_size, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2,
            "fid": "f3", "fs": fs, "fields": fields,
        })
        last_error: Optional[Exception] = None
        for host in HOSTS:
            try:
                request = urllib.request.Request(
                    host + query,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/center/"},
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
                block = (payload or {}).get("data") or {}
                return block.get("diff") or [], int(_safe_float(block.get("total")))
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"{type(last_error).__name__}: {str(last_error)[:180]}")

    def _fetch_all_pages(
        self,
        fs: str,
        fields: str,
        *,
        max_pages: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], int, List[str]]:
        first, total = self._fetch_clist_page(1, fs, fields)
        page_limit = self.max_pages if max_pages is None else max(1, int(max_pages))
        page_count = min(page_limit, max(1, math.ceil(total / self.page_size))) if total else 1
        pages: Dict[int, List[Dict[str, Any]]] = {1: first}
        errors: List[str] = []
        if page_count > 1:
            with ThreadPoolExecutor(max_workers=min(self.workers, page_count - 1)) as executor:
                futures = {
                    executor.submit(self._fetch_clist_page, page, fs, fields): page
                    for page in range(2, page_count + 1)
                }
                for future in as_completed(futures):
                    page = futures[future]
                    try:
                        pages[page] = future.result()[0]
                    except Exception as exc:
                        errors.append(f"page={page}:{type(exc).__name__}:{str(exc)[:100]}")
        rows: List[Dict[str, Any]] = []
        for page in sorted(pages):
            rows.extend(pages[page])
        return rows, total, errors

    @staticmethod
    def _excluded(name: str, board_type: str) -> bool:
        return bool(board_type == "CONCEPT" and any(pattern.lower() in name.lower() for pattern in EXCLUSION_PATTERNS))

    def _fetch_boards(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        fields = "f12,f14,f2,f3,f6,f8,f10,f15,f16,f17,f18,f62,f104,f105,f128,f136"
        rows: List[Dict[str, Any]] = []
        errors: List[str] = []
        totals: Dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(self._fetch_all_pages, fs, fields): board_type
                for board_type, fs in BOARD_SCOPES.items()
            }
            for future in as_completed(futures):
                board_type = futures[future]
                try:
                    raw_rows, total, page_errors = future.result()
                    totals[board_type] = total
                    errors.extend(f"{board_type}:{item}" for item in page_errors)
                    for raw in raw_rows:
                        name = str(raw.get("f14") or "").strip()
                        rows.append({
                            "board_code": str(raw.get("f12") or ""),
                            "board_name": name,
                            "board_type": board_type,
                            "board_price": _safe_float(raw.get("f2")),
                            "board_pct": _safe_float(raw.get("f3")) / 100.0,
                            "board_amount": _safe_float(raw.get("f6")),
                            "turnover_rate": _safe_float(raw.get("f8")),
                            "volume_ratio": _safe_float(raw.get("f10")),
                            "session_high": _safe_float(raw.get("f15")),
                            "session_low": _safe_float(raw.get("f16")),
                            "session_open": _safe_float(raw.get("f17")),
                            "previous_close": _safe_float(raw.get("f18")),
                            "main_net_inflow": _safe_float(raw.get("f62")),
                            "up_count": int(_safe_float(raw.get("f104"))),
                            "down_count": int(_safe_float(raw.get("f105"))),
                            "leading_stock": str(raw.get("f128") or ""),
                            "leading_stock_pct": _safe_float(raw.get("f136")) / 100.0,
                            "excluded": self._excluded(name, board_type),
                            "source": "eastmoney_all_market_board_clist",
                        })
                except Exception as exc:
                    errors.append(f"{board_type}:{type(exc).__name__}:{str(exc)[:140]}")
        return rows, {"totals": totals, "errors": errors}

    def _refresh_stock_cache(self, now: datetime) -> Dict[str, Any]:
        if self.stock_cache_at and (now - self.stock_cache_at).total_seconds() < self.stock_cache_seconds and self.stock_cache:
            return {"status": "CACHE", "row_count": len(self.stock_cache), "asof": self.stock_cache_at}
        fields = "f12,f14,f2,f3,f6,f8,f10,f100,f103"
        try:
            rows, total, errors = self._fetch_all_pages(
                "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", fields, max_pages=80,
            )
            cache = {}
            for raw in rows:
                code = str(raw.get("f12") or "")
                if len(code) != 6 or code.startswith(("4", "8")):
                    continue
                cache[code] = {
                    "code": code,
                    "name": str(raw.get("f14") or ""),
                    "price": _safe_float(raw.get("f2")),
                    "industry": str(raw.get("f100") or ""),
                    "concepts": _split_themes(raw.get("f103")),
                    "pct": _safe_float(raw.get("f3")) / 100.0,
                    "amount": _safe_float(raw.get("f6")),
                    "turnover_rate": _safe_float(raw.get("f8")),
                    "volume_ratio": _safe_float(raw.get("f10")),
                }
            if cache:
                self.stock_cache = cache
                self.stock_cache_at = now
            return {"status": "GREEN" if len(cache) >= 4500 else "YELLOW", "row_count": len(cache), "total": total, "errors": errors}
        except Exception as exc:
            return {"status": "ERROR", "row_count": len(self.stock_cache), "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def _fetch_limit_up_pool(self, now: datetime) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        dates = []
        # 盘前优先上一自然日；盘中先查当日。接口对非交易日返回空，再自动回退。
        start = now - timedelta(days=1) if now.strftime("%H:%M:%S") < "09:25:00" else now
        for offset in range(0, 7):
            day = (start - timedelta(days=offset)).strftime("%Y%m%d")
            if day not in dates:
                dates.append(day)
        errors = []
        for day in dates:
            try:
                query = urllib.parse.urlencode({
                    "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                    "Pageindex": 0, "pagesize": 10000, "sort": "fbt:asc", "date": day,
                })
                payload = self._http_json(LIMIT_UP_URL + query)
                raw_pool = ((payload or {}).get("data") or {}).get("pool") or []
                if not raw_pool:
                    continue
                rows = [{
                    "code": str(row.get("c") or ""),
                    "name": str(row.get("n") or ""),
                    "industry": str(row.get("hybk") or ""),
                    "streak": int(_safe_float(row.get("lbc"), 1)),
                    "amount": _safe_float(row.get("amount")),
                    "turnover_rate": _safe_float(row.get("hs")),
                    "sealed_fund": _safe_float(row.get("fund")),
                    "failed_seal_count": int(_safe_float(row.get("zbc"))),
                    "first_seal_time": str(row.get("fbt") or "").zfill(6),
                } for row in raw_pool]
                self.limit_up_cache = rows
                return rows, {
                    "status": "GREEN", "trade_date": day,
                    "is_current_trade_date": day == now.strftime("%Y%m%d"), "row_count": len(rows),
                    "source": "eastmoney_full_market_limit_up_pool",
                }
            except Exception as exc:
                errors.append(f"{day}:{type(exc).__name__}:{str(exc)[:100]}")
        return self.limit_up_cache, {
            "status": "CACHE" if self.limit_up_cache else "UNAVAILABLE",
            "row_count": len(self.limit_up_cache), "errors": errors,
            "source": "eastmoney_full_market_limit_up_pool",
        }

    def _ladder_by_theme(self, limit_rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for source in limit_rows:
            row = dict(source)
            stock = self.stock_cache.get(str(row.get("code") or ""), {})
            themes = [str(row.get("industry") or ""), str(stock.get("industry") or "")]
            themes.extend(stock.get("concepts") or [])
            for theme in dict.fromkeys(item.strip() for item in themes if item and item.strip()):
                if not self._excluded(theme, "CONCEPT"):
                    grouped[theme].append(row)
        output: Dict[str, Dict[str, Any]] = {}
        for theme, rows in grouped.items():
            streaks = [max(1, int(_safe_float(row.get("streak"), 1))) for row in rows]
            levels = sorted(set(streaks))
            first_count = sum(level == 1 for level in streaks)
            multi_count = sum(level >= 2 for level in streaks)
            max_streak = max(streaks, default=0)
            score = min(100, 9 * len(rows) + 10 * multi_count + 8 * max(max_streak - 1, 0) + 10 * int(len(levels) >= 2))
            output[theme] = {
                "limit_up_count": len(rows), "first_board_count": first_count,
                "multi_board_count": multi_count, "max_board_streak": max_streak,
                "ladder_levels": levels, "ladder_health_score": score,
                "limit_up_examples": [f"{row.get('name')}({int(_safe_float(row.get('streak'), 1))}板)" for row in sorted(rows, key=lambda x: -int(_safe_float(x.get("streak"), 1)))[:5]],
            }
        return output

    def _technical_context(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        history = list(self.history[str(row.get("board_code") or row.get("board_name"))])
        prices = [_safe_float(item.get("board_price")) for item in history] + [_safe_float(row.get("board_price"))]
        prices = [value for value in prices if value > 0]
        if len(prices) < 8:
            return {"technical_status": "WARMING_UP", "technical_score": 50, "kdj_822": None, "macd_5_10_5": None}

        k_value = d_value = 50.0
        for index in range(7, len(prices)):
            window = prices[index - 7:index + 1]
            low, high = min(window), max(window)
            rsv = (prices[index] - low) / (high - low) * 100.0 if high > low else 50.0
            k_value = 0.5 * rsv + 0.5 * k_value
            d_value = 0.5 * k_value + 0.5 * d_value
        j_value = 3.0 * k_value - 2.0 * d_value
        ema5, ema10 = _ema(prices, 5), _ema(prices, 10)
        diffs = [a - b for a, b in zip(ema5, ema10)]
        dea = _ema(diffs, 5)
        hist = diffs[-1] - dea[-1]
        prior_hist = diffs[-2] - dea[-2] if len(diffs) >= 2 else hist
        score = 50
        score += 12 if k_value > d_value else -10
        score += 10 if hist > 0 else -8
        score += 6 if hist > prior_hist else -5
        if j_value > 92:
            score -= 5
        return {
            "technical_status": "READY",
            "technical_score": max(0, min(100, score)),
            "kdj_822": {"k": round(k_value, 2), "d": round(d_value, 2), "j": round(j_value, 2)},
            "macd_5_10_5": {"dif": round(diffs[-1], 6), "dea": round(dea[-1], 6), "hist": round(hist, 6), "hist_rising": hist > prior_hist},
        }

    def _enrich_health(self, rows: List[Dict[str, Any]], ladder: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"market_median_pct": 0.0, "market_positive_breadth": 0.0, "technical_ready_count": 0}
        pct_values = sorted(row["board_pct"] for row in rows)
        amount_values = sorted(row["board_amount"] for row in rows)
        volume_ratio_values = sorted(row["volume_ratio"] for row in rows)
        turnover_values = sorted(row["turnover_rate"] for row in rows)
        inflow_ratios = sorted(row["main_net_inflow"] / max(row["board_amount"], 1.0) for row in rows)
        ladder_values = sorted(_safe_float(ladder.get(row["board_name"], {}).get("ladder_health_score")) for row in rows)
        market_median_pct = float(median(pct_values))
        market_positive_breadth = sum(value > 0 for value in pct_values) / len(pct_values)

        for row in rows:
            row.update(ladder.get(row["board_name"], {}))
            total = row["up_count"] + row["down_count"]
            row["breadth"] = row["up_count"] / total if total > 0 else 0.5
            row["leader_excess"] = row["leading_stock_pct"] - row["board_pct"]
            row["inflow_ratio"] = row["main_net_inflow"] / max(row["board_amount"], 1.0)
            row["pct_percentile"] = _percentile(row["board_pct"], pct_values)
            row["amount_percentile"] = _percentile(row["board_amount"], amount_values)
            row["volume_ratio_percentile"] = _percentile(row["volume_ratio"], volume_ratio_values)
            row["turnover_percentile"] = _percentile(row["turnover_rate"], turnover_values)
            row["inflow_percentile"] = _percentile(row["inflow_ratio"], inflow_ratios)
            row["ladder_percentile"] = _percentile(_safe_float(row.get("ladder_health_score")), ladder_values)
            row.update(self._technical_context(row))
            leader_component = max(0.0, min(1.0, 0.5 + row["leading_stock_pct"] / 0.18 - max(row["leader_excess"] - 0.08, 0.0) * 3.0))
            raw_score = 100.0 * (
                0.18 * row["pct_percentile"]
                + 0.20 * row["breadth"]
                + 0.10 * row["amount_percentile"]
                + 0.07 * row["turnover_percentile"]
                + 0.10 * row["volume_ratio_percentile"]
                + 0.10 * row["inflow_percentile"]
                + 0.13 * row["ladder_percentile"]
                + 0.07 * leader_component
                + 0.05 * (_safe_float(row.get("technical_score"), 50.0) / 100.0)
            )
            row["health_score_raw"] = round(raw_score, 2)

        scores = sorted(row["health_score_raw"] for row in rows)
        previous_top = {
            row.get("board_code") for row in self.latest.get("rows", [])
            if _safe_float(row.get("health_percentile")) >= 0.90
        }
        for row in rows:
            row["health_percentile"] = _percentile(row["health_score_raw"], scores)
            row["board_percentile"] = row["health_percentile"]

        current_top = {row.get("board_code") for row in rows if row["health_percentile"] >= 0.90}
        top_overlap = len(current_top & previous_top) / max(len(current_top | previous_top), 1) if previous_top else None
        percentile_moves = []
        for row in rows:
            key = str(row.get("board_code") or row.get("board_name"))
            history = self.history[key]
            prior = history[-1] if history else None
            row["health_delta"] = row["health_score_raw"] - _safe_float((prior or {}).get("health_score_raw"), row["health_score_raw"])
            row["percentile_delta"] = row["health_percentile"] - _safe_float((prior or {}).get("health_percentile"), row["health_percentile"])
            percentile_moves.append(abs(row["percentile_delta"]))
            recent = list(history)[-5:] + [row]
            row["top_quartile_persistence"] = sum(_safe_float(item.get("health_percentile")) >= 0.75 for item in recent) / len(recent)
            row["snapshot_count"] = len(history) + 1

        mean_percentile_move = sum(percentile_moves) / len(percentile_moves) if percentile_moves else 0.0
        fast_rotation = bool(top_overlap is not None and (top_overlap < 0.42 or mean_percentile_move >= 0.16))
        market_regime = "FAST_ROTATION" if fast_rotation else ("STABLE" if previous_top else "WARMING_UP")
        for row in rows:
            persistence = row["top_quartile_persistence"]
            concentrated = row["leader_excess"] >= 0.075 and row["breadth"] < 0.58
            if row["health_percentile"] >= 0.90 and persistence >= 0.60 and row["snapshot_count"] >= 2 and not concentrated:
                state = "SUSTAINED_LEADER"
            elif row["health_percentile"] >= 0.80 and row["percentile_delta"] >= 0.10 and row["breadth"] >= 0.55:
                state = "ROTATION_IN"
            elif row["pct_percentile"] >= 0.90 and (persistence < 0.40 or concentrated):
                state = "FLASH_HEAT"
            elif row["snapshot_count"] >= 2 and (
                row["percentile_delta"] <= -0.15
                or (row["breadth"] < 0.40 and row["main_net_inflow"] < 0)
            ):
                state = "ROTATION_OUT"
            elif row["health_percentile"] >= 0.65 and row["breadth"] >= 0.50:
                state = "HEALTHY_RISING"
            elif row["health_percentile"] <= 0.25 or (row["breadth"] < 0.40 and row["main_net_inflow"] < 0):
                state = "WEAK"
            else:
                state = "NEUTRAL"
            row["market_regime"] = market_regime
            row["rotation_state"] = state
            row["entry_support"] = bool(
                row["snapshot_count"] >= 2
                and (
                    state in {"SUSTAINED_LEADER", "HEALTHY_RISING"}
                    or (state == "ROTATION_IN" and not fast_rotation)
                )
            )
            row["rotation_caution"] = bool(state in {"FLASH_HEAT", "ROTATION_OUT"} or (fast_rotation and persistence < 0.60))
            row["market_median_pct"] = market_median_pct
            row["relative_to_market_median"] = row["board_pct"] - market_median_pct
            row["market_positive_breadth"] = market_positive_breadth
            self.history[str(row.get("board_code") or row.get("board_name"))].append({
                key: row.get(key) for key in (
                    "asof", "board_price", "board_pct", "board_amount", "breadth", "health_score_raw",
                    "health_percentile", "rotation_state", "technical_score",
                )
            })
        return {
            "market_median_pct": market_median_pct,
            "market_positive_breadth": market_positive_breadth,
            "market_regime": market_regime,
            "top_decile_overlap": top_overlap,
            "mean_percentile_move": mean_percentile_move,
            "technical_ready_count": sum(row.get("technical_status") == "READY" for row in rows),
        }

    def refresh(self) -> Dict[str, Any]:
        begin = datetime.now()
        try:
            raw_rows, board_meta = self._fetch_boards()
            seen = set()
            deduplicated = []
            for row in raw_rows:
                key = (row.get("board_type"), row.get("board_code") or row.get("board_name"))
                if key in seen:
                    continue
                seen.add(key)
                row["asof"] = begin
                deduplicated.append(row)
            eligible = [row for row in deduplicated if not row.get("excluded") and row.get("board_name")]
            excluded = [row for row in deduplicated if row.get("excluded")]
            stock_meta = self._refresh_stock_cache(begin)
            limit_rows, limit_meta = self._fetch_limit_up_pool(begin)
            ladder = self._ladder_by_theme(limit_rows)
            market = self._enrich_health(eligible, ladder)
            eligible.sort(key=lambda row: (-row["health_score_raw"], -row["board_pct"], row["board_name"]))
            for index, row in enumerate(eligible, 1):
                row["board_rank"] = index
                row["market_universe_count"] = len(eligible)
            self.latest = {
                "status": "GREEN" if eligible and stock_meta.get("status") in {"GREEN", "CACHE"} else ("YELLOW" if eligible else "EMPTY"),
                "asof": datetime.now(),
                "elapsed_ms": round((datetime.now() - begin).total_seconds() * 1000, 2),
                "row_count": len(eligible),
                "raw_row_count": len(deduplicated),
                "eligible_row_count": len(eligible),
                "excluded_row_count": len(excluded),
                "excluded_examples": [row.get("board_name") for row in excluded[:20]],
                "rows": eligible,
                "by_name": {row["board_name"]: row for row in eligible},
                "source": "eastmoney_full_market_industry_concept_health_v2",
                "scope": "ALL_MARKET_INDUSTRY_AND_STABLE_CONCEPT",
                "board_meta": board_meta,
                "stock_meta": stock_meta,
                "limit_up_meta": limit_meta,
                **market,
            }
        except Exception as exc:
            self.latest = {
                **self.latest,
                "status": "ERROR",
                "last_error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "failed_at": datetime.now(),
            }
        return self.latest

    def context_for_theme(self, theme: str) -> Dict[str, Any]:
        by_name = self.latest.get("by_name", {})
        aliases = THEME_ALIASES.get(str(theme), (str(theme),))
        exact = [
            {**by_name[alias], "matched_theme": theme, "match_type": "EXACT_ALIAS", "alias_priority": index}
            for index, alias in enumerate(aliases) if alias in by_name
        ]
        if exact:
            return min(exact, key=lambda row: int(row.get("alias_priority", 999)))
        matches = []
        for alias in aliases:
            matches.extend(
                {**row, "matched_theme": theme, "match_type": "FUZZY_ALIAS"}
                for name, row in by_name.items()
                if alias and (alias in name or name in alias)
            )
        if matches:
            matches.sort(key=lambda row: (-_safe_float(row.get("health_score_raw")), abs(len(row["board_name"]) - len(str(theme)))))
            return matches[0]
        return {"matched_theme": theme, "status": "NO_MATCH", "source": self.latest.get("source", "eastmoney_full_market")}

    def context_for_candidate(self, candidate: Mapping[str, Any]) -> Dict[str, Any]:
        # 先匹配稳定细分主题，再退到细分行业/一级行业。旧版取所有匹配中健康分最高者，
        # 容易把“锡/铜/稀土/半导体材料”错误替换成更热但更粗的“有色/电子”。
        # 板块健康必须回答该股票真正属于哪个细分方向，不能为了分数选宽泛板块。
        ordered = []
        for key in candidate.get("stable_themes") or []:
            value = str(key or "").strip()
            if value and value not in ordered:
                ordered.append(value)
        for key in (candidate.get("group_key"), candidate.get("niche"), candidate.get("subindustry"), candidate.get("primary_industry")):
            value = str(key or "").strip()
            if value and value not in ordered:
                ordered.append(value)
        matches = []
        for priority, theme in enumerate(ordered):
            context = self.context_for_theme(theme)
            if context.get("board_code"):
                matches.append({**context, "taxonomy_priority": priority})
        if matches:
            best_priority = min(int(row.get("taxonomy_priority", 999)) for row in matches)
            priority_matches = [row for row in matches if int(row.get("taxonomy_priority", 999)) == best_priority]
            best = max(priority_matches, key=lambda row: (_safe_float(row.get("health_score_raw")), _safe_float(row.get("health_percentile"))))
            return {**best, "matched_candidate_themes": [row.get("matched_theme") for row in matches]}
        return {"status": "NO_MATCH", "source": self.latest.get("source", "eastmoney_full_market")}

    def full_market_core_candidates(self, limit: int = 6) -> List[Dict[str, Any]]:
        """从全A横截面产生观察级粗筛，不把涨幅榜直接伪装成买点。

        这里只具备报价、成交活跃度和行业/概念板块健康证据，尚未完成个股日线与Tick
        深检。因此结果只能进入“全市场发现”，不能直接触发统一入场状态机。
        """

        stocks = list(self.stock_cache.values())
        if not stocks or not self.latest.get("rows"):
            return []
        amounts = sorted(_safe_float(row.get("amount")) for row in stocks)
        volume_ratios = sorted(_safe_float(row.get("volume_ratio")) for row in stocks)
        ranked = []
        for stock in stocks:
            name = str(stock.get("name") or "")
            code = str(stock.get("code") or "")
            pct = _safe_float(stock.get("pct"))
            amount = _safe_float(stock.get("amount"))
            turnover = _safe_float(stock.get("turnover_rate"))
            volume_ratio = _safe_float(stock.get("volume_ratio"))
            if (
                not code or not name or "ST" in name.upper() or "退" in name
                or _safe_float(stock.get("price")) <= 0 or amount < 50_000_000
                or pct < -0.02 or pct > 0.185 or turnover > 28
            ):
                continue
            themes = [str(stock.get("industry") or "")]
            themes.extend(str(value) for value in stock.get("concepts") or [])
            # 全市场约五千只股票，逐只做模糊板块搜索会在固定总结时造成明显延迟。
            # 此处只接受接口返回的精确行业/概念名（及显式别名），模糊映射留给33只深度池。
            by_name = self.latest.get("by_name") or {}
            matches = []
            for theme in themes:
                exact = by_name.get(theme)
                if exact:
                    matches.append({**exact, "matched_theme": theme, "match_type": "EXACT"})
                    continue
                for alias in THEME_ALIASES.get(theme, ()):
                    exact = by_name.get(alias)
                    if exact:
                        matches.append({**exact, "matched_theme": theme, "match_type": "EXACT_ALIAS"})
                        break
            if not matches:
                continue
            board = max(matches, key=lambda row: _safe_float(row.get("health_score_raw")))
            board_state = str(board.get("rotation_state") or "UNAVAILABLE")
            if (
                _safe_float(board.get("health_percentile")) < 0.80
                or board_state in {"FLASH_HEAT", "ROTATION_OUT", "WEAK"}
                or board.get("rotation_caution")
            ):
                continue
            amount_pct = _percentile(amount, amounts)
            volume_pct = _percentile(volume_ratio, volume_ratios)
            board_pct = _safe_float(board.get("board_pct"))
            relative_excess = pct - board_pct
            limit_ratio = 0.20 if code.startswith(("300", "301", "688")) else 0.10
            early_location_cap = min(0.12, limit_ratio * 0.65)
            is_named_leader = name == str(board.get("leading_stock") or "")
            entry_logic_match = bool(
                board.get("entry_support")
                and not board.get("rotation_caution")
                and _safe_float(board.get("health_percentile")) >= 0.82
                and pct >= 0.005
                and pct <= early_location_cap
                and relative_excess >= -0.002
                and amount_pct >= 0.55
                and (volume_ratio >= 1.0 or volume_pct >= 0.60)
            )
            # 涨幅只贡献很小的一部分，并在极端拉升后扣分；主权重仍是板块健康、
            # 持续性和成交容量，避免退化成实时涨幅榜。
            location_score = 1.0 if 0.01 <= pct <= 0.07 else (0.65 if -0.005 <= pct <= 0.11 else 0.25)
            score = 100.0 * (
                0.48 * _safe_float(board.get("health_percentile"), 0.5)
                + 0.20 * _safe_float(board.get("top_quartile_persistence"), 0.0)
                + 0.14 * amount_pct
                + 0.08 * volume_pct
                + 0.10 * location_score
            )
            ranked.append({
                **stock,
                "symbol": ("SHSE." if code.startswith("6") else "SZSE.") + code,
                "discovery_score": int(round(_clip(score, 0.0, 100.0))),
                "matched_board": board,
                "discovery_only": True,
                "formal_buy_eligible": False,
                "entry_logic_match": entry_logic_match,
                "strategy_match_grade": "A_EARLY_LEADER" if entry_logic_match and is_named_leader else ("B_STRONG_FRONT" if entry_logic_match else "C_BOARD_WATCH"),
                "is_board_named_leader": is_named_leader,
                "relative_excess_vs_board": relative_excess,
                "amount_percentile": amount_pct,
                "volume_ratio_percentile": volume_pct,
                "early_location_cap": early_location_cap,
                "next_validation": (
                    "日线趋势/修复合格；实时站稳VWAP；5/15分钟同向；持续资金承接；30/60分钟无顶背离；T+1生存门通过"
                ),
                "cancel_when": "板块轮出、个股跌破VWAP且不能收复、资金确认流出，或涨幅越过早期位置上限",
                "reason": (
                    "强板块前排、相对强度、成交容量和早期位置已通过一级筛选；尚待日线与Tick深检"
                    if entry_logic_match else
                    "全市场强板块观察股；个股早期位置/相对强度/量能至少一项尚未通过"
                ),
            })
        ranked.sort(key=lambda row: (
            -int(bool(row.get("entry_logic_match"))),
            -int(bool(row.get("is_board_named_leader"))),
            -int(row["discovery_score"]),
            -_safe_float(row.get("amount")),
            row["code"],
        ))
        # 同一板块最多保留2只，防止一个热门方向垄断全部展示位。
        selected, board_counts = [], defaultdict(int)
        for row in ranked:
            board_name = str((row.get("matched_board") or {}).get("board_name") or "")
            if board_counts[board_name] >= 2:
                continue
            selected.append(row)
            board_counts[board_name] += 1
            if len(selected) >= max(0, int(limit)):
                break
        return selected
