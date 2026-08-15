# coding: utf-8
"""最近10个交易日日线高/低点与当日实时最高价的特殊数字旁路TAG插件。

插件读取股票APP常见前复权口径的完整日线：最高价用于原有特殊数字提醒；最低价在命中特殊数字、
随后连续3个交易日回踩支撑区且始终未跌破时，输出“可能做底”观察TAG。它不返回BUY/SELL，
不修改候选、信号强度、行动指令、持仓台账或主状态机，也不调用订单接口。
"""

from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd


PLUGIN_VERSION = "numeric_extreme_tag_plugin_v5_dynamic_support_shadow"
DEFAULT_LOOKBACK_TRADING_DAYS = 10
DEFAULT_LOW_SUPPORT_CONFIRM_DAYS = 3
DEFAULT_LOW_SUPPORT_TEST_TOLERANCE_PCT = 0.01

PATTERN_PRIORITY = {
    "LEOPARD_RUN4_PLUS": 6,
    "SPLIT_SIDE_5_AND_10": 5,
    "LEOPARD_RUN3": 4,
    "PAIR_PATTERN": 3,
    "PAIR_TAIL": 2,
    "DUANHUN_CODE": 1,
    "LEOPARD_BROAD": 0,
}

PATTERN_LABELS = {
    "LEOPARD_RUN4_PLUS": "超级豹子（四连及以上）",
    "SPLIT_SIDE_5_AND_10": "小数点左右分区5/10规则",
    "LEOPARD_RUN3": "严格豹子（三连）",
    "PAIR_PATTERN": "结构对子（ABAB/AABB）",
    "PAIR_TAIL": "尾数对子",
    "DUANHUN_CODE": "断魂码（全部数字和尾数5）",
    "LEOPARD_BROAD": "宽口径豹子（只审计）",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def infer_price_precision(symbol: str) -> int:
    """按品种推断显示精度；有证券元数据时调用方可显式覆盖。"""

    market, _, code = str(symbol or "").partition(".")
    if market == "SHSE" and code.startswith("5"):
        return 3
    if market == "SZSE" and code.startswith(("15", "16")):
        return 3
    return 2


def format_raw_price(price: Any, precision: int) -> str:
    value = Decimal(str(price))
    quantum = Decimal("1").scaleb(-precision)
    return f"{value.quantize(quantum, rounding=ROUND_HALF_UP):.{precision}f}"


def _display_price_value(price: Any, precision: int) -> float:
    """按APP显示精度比较价格，避免浮点或复权尾差制造假跌破。"""

    return float(format_raw_price(price, precision))


def _multiple(value: int, divisor: int) -> bool:
    # 工程口径只接受正倍数；00不单独构成“10的倍数”，避免所有整数最高价都被该规则命中。
    return value > 0 and value % divisor == 0


def _side_qualification(side_text: str) -> Dict[str, Any]:
    """段值本身优先；段值不满足时，再用该段各位数字之和。"""

    value = int(side_text or "0")
    digit_sum = sum(int(char) for char in (side_text or "0"))
    result: Dict[str, Any] = {
        "text": side_text,
        "value": value,
        "digit_sum": digit_sum,
        "multiple_5": False,
        "multiple_10": False,
        "multiple_5_basis": None,
        "multiple_10_basis": None,
    }
    if _multiple(value, 5):
        result["multiple_5"] = True
        result["multiple_5_basis"] = f"段值{value}是5的倍数"
    elif _multiple(digit_sum, 5):
        result["multiple_5"] = True
        result["multiple_5_basis"] = f"数字和{'+'.join(side_text)}={digit_sum}是5的倍数"

    if _multiple(value, 10):
        result["multiple_10"] = True
        result["multiple_10_basis"] = f"段值{value}是10的倍数"
    elif _multiple(digit_sum, 10):
        result["multiple_10"] = True
        result["multiple_10_basis"] = f"数字和{'+'.join(side_text)}={digit_sum}是10的倍数"
    return result


def classify_split_side_5_and_10(price_text: str) -> Dict[str, Any]:
    """验证用户定义的“小数点左右一边5倍数、另一边10倍数”规则。"""

    left_text, _, right_text = price_text.partition(".")
    left = _side_qualification(left_text)
    right = _side_qualification(right_text)
    left5_right10 = bool(left["multiple_5"] and right["multiple_10"])
    left10_right5 = bool(left["multiple_10"] and right["multiple_5"])
    matched = left5_right10 or left10_right5
    if left5_right10:
        orientation = "LEFT_5_RIGHT_10"
        explanation = f"左侧：{left['multiple_5_basis']}；右侧：{right['multiple_10_basis']}"
    elif left10_right5:
        orientation = "LEFT_10_RIGHT_5"
        explanation = f"左侧：{left['multiple_10_basis']}；右侧：{right['multiple_5_basis']}"
    else:
        orientation = None
        explanation = "小数点左右未形成一边5倍数、另一边10倍数"
    return {
        "matched": matched,
        "orientation": orientation,
        "left": left,
        "right": right,
        "explanation": explanation,
    }


def classify_numeric_patterns(price: Any, precision: int) -> Dict[str, Any]:
    """对前复权日线显示的日最高价做确定性分类。"""

    price_text = format_raw_price(price, precision)
    digits = price_text.replace(".", "").replace("-", "")
    matched: List[str] = []

    longest_run = 0
    current_run = 0
    previous = None
    for char in digits:
        current_run = current_run + 1 if char == previous else 1
        previous = char
        longest_run = max(longest_run, current_run)
    if longest_run >= 4:
        matched.append("LEOPARD_RUN4_PLUS")
    elif longest_run >= 3:
        matched.append("LEOPARD_RUN3")

    split_rule = classify_split_side_5_and_10(price_text)
    if split_rule["matched"]:
        matched.append("SPLIT_SIDE_5_AND_10")

    tail4 = digits[-4:] if len(digits) >= 4 else ""
    if len(tail4) == 4:
        a, b, c, d = tail4
        is_abab = a == c and b == d and a != b
        is_aabb = a == b and c == d and a != c
        if is_abab or is_aabb:
            matched.append("PAIR_PATTERN")
    if len(digits) >= 2 and digits[-1] == digits[-2]:
        matched.append("PAIR_TAIL")

    counts = Counter(digits)
    if counts and max(counts.values()) >= 3:
        matched.append("LEOPARD_BROAD")

    digit_sum = sum(int(char) for char in digits)
    if digit_sum % 10 == 5:
        matched.append("DUANHUN_CODE")

    matched = sorted(set(matched), key=lambda value: (-PATTERN_PRIORITY[value], value))
    primary = next((value for value in matched if value != "LEOPARD_BROAD"), None)
    return {
        "price_text": price_text,
        "digits": digits,
        "digit_sum": digit_sum,
        "digit_sum_mod10": digit_sum % 10,
        "longest_run": longest_run,
        "matched_patterns": matched,
        "matched_patterns_cn": [PATTERN_LABELS.get(value, value) for value in matched],
        "primary_pattern": primary,
        "primary_pattern_cn": PATTERN_LABELS.get(primary, primary) if primary else None,
        "split_side_5_and_10": split_rule,
        "has_tag": bool(primary),
    }


class NumericPatternTagPlugin:
    """扫描最近N个完整交易日的前复权高/低点，并保存旁路TAG快照。"""

    def __init__(
        self,
        lookback_trading_days: int = DEFAULT_LOOKBACK_TRADING_DAYS,
        low_support_confirm_days: int = DEFAULT_LOW_SUPPORT_CONFIRM_DAYS,
        low_support_test_tolerance_pct: float = DEFAULT_LOW_SUPPORT_TEST_TOLERANCE_PCT,
    ):
        self.lookback_trading_days = int(lookback_trading_days)
        self.low_support_confirm_days = max(1, int(low_support_confirm_days))
        self.low_support_test_tolerance_pct = max(0.0, float(low_support_test_tolerance_pct))
        self.latest_scan: Dict[str, Any] = {
            "status": "NOT_READY",
            "plugin_version": PLUGIN_VERSION,
            "strategy_effect": "NONE_TAG_ONLY",
            "lookback_trading_days": self.lookback_trading_days,
            "low_support_confirm_days": self.low_support_confirm_days,
            "low_support_test_tolerance_pct": self.low_support_test_tolerance_pct,
            "by_symbol": {},
            "hits": [],
            "low_support_hits": [],
        }
        self.intraday_scan: Dict[str, Any] = {
            "status": "NOT_READY",
            "trade_date": None,
            "plugin_version": PLUGIN_VERSION,
            "strategy_effect": "NONE_TAG_ONLY",
            "by_symbol": {},
            "hits": [],
        }

    @staticmethod
    def _first_consecutive_true_window(flags: Sequence[bool], length: int) -> Optional[int]:
        run = 0
        for index, flag in enumerate(flags):
            run = run + 1 if flag else 0
            if run >= length:
                return index - length + 1
        return None

    def _scan_symbol_low_supports(
        self,
        symbol: str,
        frame: pd.DataFrame,
        *,
        name: str,
        precision: int,
        source: str,
        asof: str,
    ) -> List[Dict[str, Any]]:
        """找特殊数字低点支撑的A/B/C三种影子确认；只返回研究TAG。

        真实OHLC齐全时使用ATR自适应支撑带和承接证据；只有测试/残缺数据时才退回
        原1%配置。单纯远离支撑而没有真实下探，不累计确认次数。
        """

        if "low" not in frame.columns:
            return []
        low_rows = frame.dropna(subset=["low"]).copy()
        low_rows = low_rows[low_rows["low"] > 0].reset_index(drop=True)
        if len(low_rows) <= self.low_support_confirm_days:
            return []

        display_lows = [
            _display_price_value(value, precision)
            for value in low_rows["low"].tolist()
        ]
        tick_size = 10 ** (-precision)
        has_ohlc = all(column in low_rows.columns for column in ("open", "high", "close"))
        if has_ohlc and "_atr14" not in low_rows.columns:
            previous_close = pd.to_numeric(low_rows["close"], errors="coerce").shift(1)
            true_range = pd.concat([
                pd.to_numeric(low_rows["high"], errors="coerce") - pd.to_numeric(low_rows["low"], errors="coerce"),
                (pd.to_numeric(low_rows["high"], errors="coerce") - previous_close).abs(),
                (pd.to_numeric(low_rows["low"], errors="coerce") - previous_close).abs(),
            ], axis=1).max(axis=1)
            low_rows["_atr14"] = true_range.rolling(14, min_periods=5).mean()
        low_support_hits: List[Dict[str, Any]] = []
        for anchor_index in range(0, len(low_rows) - self.low_support_confirm_days):
            anchor_row = low_rows.iloc[anchor_index]
            anchor_low = display_lows[anchor_index]
            classification = classify_numeric_patterns(anchor_low, precision)
            if not classification["has_tag"]:
                continue
            # 锚点在当时必须至少是近5日低点，不能把普通中继低价伪装成“做底”。
            prior_lows = display_lows[max(0, anchor_index - 4):anchor_index + 1]
            if prior_lows and anchor_low > min(prior_lows):
                continue

            later_rows = low_rows.iloc[anchor_index + 1:].reset_index(drop=True)
            later_lows = display_lows[anchor_index + 1:]
            # “一直没有跌破”覆盖锚点后的全部完整交易日，而不只覆盖确认窗口。
            if any(value < anchor_low for value in later_lows):
                continue
            atr_value = _safe_float(anchor_row.get("_atr14")) if has_ohlc else 0.0
            if atr_value > 0:
                atr_pct = atr_value / anchor_low
                band_pct = min(0.012, max(2.0 * tick_size / anchor_low, 0.25 * atr_pct))
                band_method = "ATR14_ADAPTIVE_0.25_CAP_1.2PCT"
            else:
                band_pct = self.low_support_test_tolerance_pct
                band_method = "FALLBACK_CONFIGURED_PCT_INCOMPLETE_OHLC"
            support_band_upper = _display_price_value(anchor_low * (1.0 + band_pct), precision)
            held_flags = [value >= anchor_low for value in later_lows]
            retest_flags = []
            acceptance_flags = []
            for offset, value in enumerate(later_lows):
                row = later_rows.iloc[offset]
                in_band = anchor_low <= value <= support_band_upper
                if has_ohlc:
                    row_open = _safe_float(row.get("open"))
                    row_high = _safe_float(row.get("high"))
                    row_close = _safe_float(row.get("close"))
                    prior_close = _safe_float(low_rows.iloc[anchor_index + offset].get("close"))
                    approached = prior_close >= anchor_low * (1.0 + 0.25 * band_pct)
                    lower_wick = max(0.0, min(row_open, row_close) - value)
                    day_range = max(tick_size, row_high - value)
                    accepted = bool(
                        row_close >= anchor_low * (1.0 + 0.25 * band_pct)
                        and (row_close >= row_open or lower_wick / day_range >= 0.30)
                    )
                    retest_flags.append(bool(in_band and approached and accepted))
                    acceptance_flags.append(accepted)
                else:
                    retest_flags.append(bool(in_band))
                    acceptance_flags.append(bool(in_band))

            variants: List[str] = []
            confirmation_offset: Optional[int] = None
            first_three_held = len(held_flags) >= 3 and all(held_flags[:3])
            if first_three_held and all(retest_flags[:3]):
                variants.append("A_STRICT_3_CONSECUTIVE_RETESTS")
                confirmation_offset = 2
            if first_three_held and sum(retest_flags[:3]) >= 2:
                variants.append("B_3DAY_OBSERVE_2_VALID_RETESTS")
                confirmation_offset = 2 if confirmation_offset is None else min(confirmation_offset, 2)
            retest_count = 0
            c_offset = None
            for offset, (held, retested) in enumerate(zip(held_flags[:10], retest_flags[:10])):
                if not held:
                    break
                retest_count += int(retested)
                if retest_count >= 3:
                    c_offset = offset
                    break
            if c_offset is not None:
                variants.append("C_10DAY_3_VALID_RETESTS")
                confirmation_offset = c_offset if confirmation_offset is None else min(confirmation_offset, c_offset)
            if not variants or confirmation_offset is None:
                continue
            test_rows = later_rows.iloc[:confirmation_offset + 1]
            valid_indexes = [index for index in range(confirmation_offset + 1) if retest_flags[index]]
            test_lows = [later_lows[index] for index in valid_indexes]
            anchor_date = str(anchor_row["eob"].date())
            confirmation_date = str(test_rows.iloc[-1]["eob"].date())
            tag = {
                "event": "NUMERIC_DAILY_LOW_SUPPORT_TAG",
                "event_id": f"{anchor_date}:{symbol}:DAILY_LOW_SUPPORT:{classification['price_text']}:{confirmation_date}",
                "symbol": symbol,
                "name": name,
                "anchor_trade_date": anchor_date,
                "trade_date": anchor_date,
                "confirmation_trade_date": confirmation_date,
                "unbroken_through": str(asof)[:10],
                "trading_days_ago": len(low_rows) - anchor_index - 1,
                "daily_low": anchor_low,
                "support_price": anchor_low,
                "price_text": classification["price_text"],
                "price_precision": precision,
                "primary_pattern": classification["primary_pattern"],
                "primary_pattern_cn": classification["primary_pattern_cn"],
                "matched_patterns": classification["matched_patterns"],
                "matched_patterns_cn": classification["matched_patterns_cn"],
                "digit_sum": classification["digit_sum"],
                "split_side_5_and_10": classification["split_side_5_and_10"],
                "confirmation_days": confirmation_offset + 1,
                "support_variants": variants,
                "valid_retest_count": len(valid_indexes),
                "test_dates": [str(test_rows.iloc[index]["eob"].date()) for index in valid_indexes],
                "test_lows": test_lows,
                "test_low_texts": [format_raw_price(value, precision) for value in test_lows],
                "support_band_upper": support_band_upper,
                "support_band_upper_text": format_raw_price(support_band_upper, precision),
                "support_test_tolerance_pct": band_pct,
                "support_band_method": band_method,
                "atr14_at_anchor": atr_value or None,
                "retest_flags": retest_flags[:confirmation_offset + 1],
                "acceptance_flags": acceptance_flags[:confirmation_offset + 1],
                "all_subsequent_daily_lows_held": True,
                "subsequent_min_low": min(later_lows),
                "possible_bottom": True,
                "research_status": "UNVALIDATED_OBSERVATION_ONLY",
                "experimental_weight": 0,
                "source": source,
                "price_adjustment": "ADJUST_PREV/front-adjusted",
                "display_semantics": "CURRENT_APP_FRONT_ADJUSTED_VIEW",
                "historical_values_may_restate_after_future_corporate_actions": True,
                "uses_completed_daily_low_only": True,
                "strategy_effect": "NONE_TAG_ONLY",
                "changes_main_signal": False,
                "changes_candidate_rank": False,
                "changes_signal_strength": False,
                "changes_action_decision": False,
                "changes_position_ledger": False,
                "order_submitted": False,
                "plugin_version": PLUGIN_VERSION,
            }
            low_support_hits.append(tag)
        low_support_hits.sort(
            key=lambda row: (row["confirmation_trade_date"], row["anchor_trade_date"]),
            reverse=True,
        )
        return low_support_hits

    def scan_recent_daily_extremes(
        self,
        frames: Dict[str, pd.DataFrame],
        *,
        asof: str,
        names: Optional[Dict[str, str]] = None,
        source: str = "GOLDMINER_HISTORY_RANGE_BATCH_ADJUST_PREV",
    ) -> Dict[str, Any]:
        names = names or {}
        hits: List[Dict[str, Any]] = []
        low_support_hits: List[Dict[str, Any]] = []
        by_symbol: Dict[str, Dict[str, Any]] = {}
        coverage_rows = []
        for symbol, raw in frames.items():
            frame = raw.copy() if raw is not None else pd.DataFrame()
            available_price_columns = [column for column in ("high", "low") if column in frame.columns]
            if not len(frame) or "eob" not in frame.columns or not available_price_columns:
                by_symbol[symbol] = {
                    "symbol": symbol,
                    "status": "NO_RAW_DAILY_DATA",
                    "hits": [],
                    "low_support_hits": [],
                }
                continue
            frame["eob"] = pd.to_datetime(frame["eob"], errors="coerce")
            for column in available_price_columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna(subset=["eob"])
            frame = frame[frame["eob"].dt.strftime("%Y-%m-%d") <= str(asof)[:10]]
            frame = frame.sort_values("eob").drop_duplicates("eob", keep="last")
            # 支撑观察仍只看最近10个交易日，但ATR必须使用锚点之前的数据计算，
            # 不能先裁成10日再把短窗口波动冒充ATR14。
            if all(column in frame.columns for column in ("high", "low", "close")):
                previous_close = pd.to_numeric(frame["close"], errors="coerce").shift(1)
                true_range = pd.concat([
                    pd.to_numeric(frame["high"], errors="coerce") - pd.to_numeric(frame["low"], errors="coerce"),
                    (pd.to_numeric(frame["high"], errors="coerce") - previous_close).abs(),
                    (pd.to_numeric(frame["low"], errors="coerce") - previous_close).abs(),
                ], axis=1).max(axis=1)
                frame["_atr14"] = true_range.rolling(14, min_periods=5).mean()
            frame = frame.tail(self.lookback_trading_days)
            precision = infer_price_precision(symbol)
            symbol_hits = []
            if "high" in frame.columns:
                for offset, (_, row) in enumerate(frame.iloc[::-1].iterrows()):
                    high = _safe_float(row.get("high"))
                    if high <= 0:
                        continue
                    classification = classify_numeric_patterns(high, precision)
                    if not classification["has_tag"]:
                        continue
                    tag = {
                        "event": "NUMERIC_DAILY_HIGH_TAG",
                        "event_id": f"{str(row['eob'].date())}:{symbol}:DAILY_HIGH:{classification['price_text']}",
                        "symbol": symbol,
                        "name": names.get(symbol, ""),
                        "trade_date": str(row["eob"].date()),
                        "trading_days_ago": offset,
                        "daily_high": high,
                        "price_text": classification["price_text"],
                        "price_precision": precision,
                        "primary_pattern": classification["primary_pattern"],
                        "primary_pattern_cn": classification["primary_pattern_cn"],
                        "matched_patterns": classification["matched_patterns"],
                        "matched_patterns_cn": classification["matched_patterns_cn"],
                        "digit_sum": classification["digit_sum"],
                        "split_side_5_and_10": classification["split_side_5_and_10"],
                        "source": source,
                        "price_adjustment": "ADJUST_PREV/front-adjusted",
                        "display_semantics": "CURRENT_APP_FRONT_ADJUSTED_VIEW",
                        "historical_values_may_restate_after_future_corporate_actions": True,
                        "uses_completed_daily_high_only": True,
                        "strategy_effect": "NONE_TAG_ONLY",
                        "changes_main_signal": False,
                        "changes_signal_strength": False,
                        "changes_action_decision": False,
                        "changes_position_ledger": False,
                        "order_submitted": False,
                        "plugin_version": PLUGIN_VERSION,
                    }
                    symbol_hits.append(tag)
                    hits.append(tag)
            symbol_low_support_hits = self._scan_symbol_low_supports(
                symbol,
                frame,
                name=names.get(symbol, ""),
                precision=precision,
                source=source,
                asof=asof,
            )
            low_support_hits.extend(symbol_low_support_hits)
            symbol_hits.sort(key=lambda row: row["trade_date"], reverse=True)
            by_symbol[symbol] = {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "status": "READY",
                "asof": str(asof)[:10],
                "scanned_trading_days": len(frame),
                "hit_count": len(symbol_hits),
                "has_recent_tag": bool(symbol_hits),
                "latest_hit": symbol_hits[0] if symbol_hits else None,
                "hits": symbol_hits,
                "low_support_hit_count": len(symbol_low_support_hits),
                "has_low_support_tag": bool(symbol_low_support_hits),
                "latest_low_support_hit": symbol_low_support_hits[0] if symbol_low_support_hits else None,
                "low_support_hits": symbol_low_support_hits,
                "strategy_effect": "NONE_TAG_ONLY",
            }
            coverage_rows.append({
                "symbol": symbol,
                "scanned_trading_days": len(frame),
                "daily_high_ready": bool("high" in frame.columns and frame["high"].notna().any()),
                "daily_low_support_ready": bool("low" in frame.columns and frame["low"].notna().any()),
            })

        hits.sort(key=lambda row: (row["trade_date"], PATTERN_PRIORITY.get(row["primary_pattern"], 0)), reverse=True)
        low_support_hits.sort(
            key=lambda row: (
                row["confirmation_trade_date"],
                row["anchor_trade_date"],
                PATTERN_PRIORITY.get(row["primary_pattern"], 0),
            ),
            reverse=True,
        )
        self.latest_scan = {
            "status": "READY" if frames else "NO_DATA",
            "plugin_version": PLUGIN_VERSION,
            "strategy_effect": "NONE_TAG_ONLY",
            "asof": str(asof)[:10],
            "source": source,
            "price_adjustment": "ADJUST_PREV/front-adjusted",
            "display_semantics": "CURRENT_APP_FRONT_ADJUSTED_VIEW",
            "historical_values_may_restate_after_future_corporate_actions": True,
            "lookback_trading_days": self.lookback_trading_days,
            "scan_scope": ["DAILY_HIGH", "DAILY_LOW_SUPPORT"],
            "low_support_confirm_days": self.low_support_confirm_days,
            "low_support_test_tolerance_pct": self.low_support_test_tolerance_pct,
            "symbol_count": len(frames),
            "ready_symbol_count": sum(row.get("status") == "READY" for row in by_symbol.values()),
            "daily_low_support_ready_symbol_count": sum(
                bool(row.get("daily_low_support_ready")) for row in coverage_rows
            ),
            "tagged_symbol_count": sum(bool(row.get("has_recent_tag")) for row in by_symbol.values()),
            "hit_count": len(hits),
            "low_support_tagged_symbol_count": sum(bool(row.get("has_low_support_tag")) for row in by_symbol.values()),
            "low_support_hit_count": len(low_support_hits),
            "coverage": coverage_rows,
            "by_symbol": by_symbol,
            "hits": hits,
            "low_support_hits": low_support_hits,
        }
        return self.latest_scan

    def scan_recent_daily_highs(
        self,
        frames: Dict[str, pd.DataFrame],
        *,
        asof: str,
        names: Optional[Dict[str, str]] = None,
        source: str = "GOLDMINER_HISTORY_RANGE_BATCH_ADJUST_PREV",
    ) -> Dict[str, Any]:
        """兼容旧调用名；V4实际同时扫描日线最高价与低点支撑。"""

        return self.scan_recent_daily_extremes(frames, asof=asof, names=names, source=source)

    def set_unavailable(self, asof: str, error: str) -> Dict[str, Any]:
        self.latest_scan = {
            "status": "UNAVAILABLE",
            "plugin_version": PLUGIN_VERSION,
            "strategy_effect": "NONE_TAG_ONLY",
            "asof": str(asof)[:10],
            "lookback_trading_days": self.lookback_trading_days,
            "low_support_confirm_days": self.low_support_confirm_days,
            "low_support_test_tolerance_pct": self.low_support_test_tolerance_pct,
            "error": str(error)[:300],
            "by_symbol": {},
            "hits": [],
            "low_support_hits": [],
        }
        return self.latest_scan

    def context_for(self, symbol: str) -> Dict[str, Any]:
        return (self.latest_scan.get("by_symbol") or {}).get(symbol, {
            "symbol": symbol,
            "status": self.latest_scan.get("status", "NOT_READY"),
            "hits": [],
            "low_support_hits": [],
            "strategy_effect": "NONE_TAG_ONLY",
        })

    def recent_hits(self, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self.latest_scan.get("hits") or [])[: max(0, int(limit))]

    def recent_low_support_hits(self, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self.latest_scan.get("low_support_hits") or [])[: max(0, int(limit))]

    def update_intraday_high(
        self,
        symbol: str,
        name: str,
        trade_date: str,
        event_ts: str,
        price: Any,
        *,
        source: str = "GOLDMINER_TICK_SESSION_HIGH",
    ) -> Optional[Dict[str, Any]]:
        """当价格创当日新高时检查一次；只返回新命中的旁路TAG。

        未命中特殊数字的新高也会更新session_high，后续低价不会被误当成当日最高价。
        同一显示价格只记录一次，不触发BUY/SELL，也不改变任何主策略字段。
        """

        trade_date = str(trade_date)[:10]
        if self.intraday_scan.get("trade_date") != trade_date:
            self.intraday_scan = {
                "status": "READY",
                "trade_date": trade_date,
                "plugin_version": PLUGIN_VERSION,
                "strategy_effect": "NONE_TAG_ONLY",
                "by_symbol": {},
                "hits": [],
            }
        value = _safe_float(price)
        if not symbol or value <= 0:
            return None
        by_symbol = self.intraday_scan["by_symbol"]
        context = by_symbol.setdefault(symbol, {
            "symbol": symbol,
            "name": name,
            "trade_date": trade_date,
            "session_high": 0.0,
            "hit_count": 0,
            "has_intraday_tag": False,
            "latest_hit": None,
            "hits": [],
            "strategy_effect": "NONE_TAG_ONLY",
        })
        if value <= _safe_float(context.get("session_high")):
            return None
        context["session_high"] = value
        precision = infer_price_precision(symbol)
        classification = classify_numeric_patterns(value, precision)
        if not classification["has_tag"]:
            return None
        event_id = f"{trade_date}:{symbol}:INTRADAY_HIGH:{classification['price_text']}"
        if any(row.get("event_id") == event_id for row in context["hits"]):
            return None
        tag = {
            "event": "NUMERIC_INTRADAY_HIGH_TAG",
            "event_id": event_id,
            "symbol": symbol,
            "name": name,
            "trade_date": trade_date,
            "event_ts": str(event_ts),
            "intraday_high": value,
            "price_text": classification["price_text"],
            "price_precision": precision,
            "primary_pattern": classification["primary_pattern"],
            "primary_pattern_cn": classification["primary_pattern_cn"],
            "matched_patterns": classification["matched_patterns"],
            "matched_patterns_cn": classification["matched_patterns_cn"],
            "digit_sum": classification["digit_sum"],
            "split_side_5_and_10": classification["split_side_5_and_10"],
            "source": source,
            "uses_running_session_high_only": True,
            "strategy_effect": "NONE_TAG_ONLY",
            "changes_main_signal": False,
            "changes_signal_strength": False,
            "changes_action_decision": False,
            "changes_position_ledger": False,
            "order_submitted": False,
            "plugin_version": PLUGIN_VERSION,
        }
        context["hits"].insert(0, tag)
        context["hit_count"] = len(context["hits"])
        context["has_intraday_tag"] = True
        context["latest_hit"] = tag
        self.intraday_scan["hits"].insert(0, tag)
        return tag

    def intraday_context_for(self, symbol: str) -> Dict[str, Any]:
        return (self.intraday_scan.get("by_symbol") or {}).get(symbol, {
            "symbol": symbol,
            "trade_date": self.intraday_scan.get("trade_date"),
            "status": self.intraday_scan.get("status", "NOT_READY"),
            "session_high": 0.0,
            "hit_count": 0,
            "has_intraday_tag": False,
            "latest_hit": None,
            "hits": [],
            "strategy_effect": "NONE_TAG_ONLY",
        })

    def recent_intraday_hits(self, limit: int = 10) -> List[Dict[str, Any]]:
        return list(self.intraday_scan.get("hits") or [])[: max(0, int(limit))]

    def snapshot(self) -> Dict[str, Any]:
        return {**self.latest_scan, "intraday": self.intraday_scan}


def _split_explanation(tag: Dict[str, Any]) -> str:
    if "SPLIT_SIDE_5_AND_10" not in (tag.get("matched_patterns") or []):
        return ""
    split = tag.get("split_side_5_and_10") or {}
    return str(split.get("explanation") or "")


def inline_tag_text(context: Optional[Dict[str, Any]]) -> str:
    if not context:
        return ""
    lines = []
    if context.get("has_recent_tag"):
        latest = context.get("latest_hit") or {}
        detail = _split_explanation(latest)
        lines.append(
            f"高点辅助TAG：近10日命中{context.get('hit_count', 0)}次｜最近{latest.get('trade_date')} "
            f"最高{latest.get('price_text')}·{latest.get('primary_pattern_cn')}"
            + (f"（{detail}）" if detail else "")
        )
    if context.get("has_low_support_tag"):
        latest_low = context.get("latest_low_support_hit") or {}
        lines.append(
            f"低点支撑TAG：{latest_low.get('anchor_trade_date')}最低{latest_low.get('price_text')}命中"
            f"{latest_low.get('primary_pattern_cn')}，{latest_low.get('valid_retest_count', 0)}次有效下探承接、"
            f"确认版本{'/'.join(latest_low.get('support_variants') or [])}，可能做底｜观察权重0"
        )
    if not lines:
        return ""
    return "\n".join(lines) + "｜仅供人工复核，不改变本条主信号和行动指令。"


def intraday_inline_tag_text(context: Optional[Dict[str, Any]]) -> str:
    if not context or not context.get("has_intraday_tag"):
        return ""
    latest = context.get("latest_hit") or {}
    detail = _split_explanation(latest)
    return (
        f"日内高点TAG：今日运行最高价{latest.get('price_text')}命中{latest.get('primary_pattern_cn')}"
        + (f"（{detail}）" if detail else "")
        + "｜仅供人工复核，不改变信号和动作。"
    )


def candidate_tag_line(context: Optional[Dict[str, Any]]) -> str:
    if not context or context.get("status") != "READY":
        return "特殊数字TAG：未取得可靠的前复权近10日日线数据；不影响主策略。"
    lines = []
    if context.get("has_recent_tag"):
        latest = context.get("latest_hit") or {}
        detail = _split_explanation(latest)
        lines.append(
            f"高点：近10日命中{context.get('hit_count', 0)}次；最近{latest.get('trade_date')} "
            f"最高{latest.get('price_text')}（{latest.get('primary_pattern_cn')}）"
            + (f"；计算：{detail}" if detail else "")
        )
    if context.get("has_low_support_tag"):
        latest_low = context.get("latest_low_support_hit") or {}
        lines.append(
            f"低点支撑：{latest_low.get('anchor_trade_date')}最低{latest_low.get('price_text')}（"
            f"{latest_low.get('primary_pattern_cn')}），{latest_low.get('confirmation_trade_date')}确认连续"
            f"{latest_low.get('valid_retest_count', 0)}次有效下探承接，可能做底；观察权重0"
        )
    if not lines:
        return "特殊数字TAG：近10日日线高点未命中，低点也未形成连续3日回踩不破；不影响主策略。"
    return "特殊数字TAG：" + "｜".join(lines) + "。仅提醒人工复核，不改变动作。"


def summary_tag_line(tag: Dict[str, Any], code: str) -> str:
    detail = _split_explanation(tag)
    return (
        f"- {tag.get('name') or '未知'}（{code}）｜{tag.get('trade_date')}最高{tag.get('price_text')}｜"
        f"{tag.get('primary_pattern_cn')}"
        + (f"｜{detail}" if detail else "")
        + "\n  动作影响：无；只做人工复核，不改变主策略。"
    )


def summary_low_support_tag_line(tag: Dict[str, Any], code: str) -> str:
    test_dates = tag.get("test_dates") or []
    test_range = (
        f"{test_dates[0]}至{test_dates[-1]}"
        if len(test_dates) >= 2
        else (test_dates[0] if test_dates else "确认窗口未知")
    )
    return (
        f"- {tag.get('name') or '未知'}（{code}）｜{tag.get('anchor_trade_date')}最低{tag.get('price_text')}｜"
        f"{tag.get('primary_pattern_cn')}\n"
        f"  {test_range}累计{tag.get('valid_retest_count', 0)}次有效下探承接｜"
        f"版本{'/'.join(tag.get('support_variants') or [])}｜可能做底；"
        f"观察权重{tag.get('experimental_weight', 0)}，不改变主策略和动作。"
    )
