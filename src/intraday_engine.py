# coding: utf-8
"""基于真实时间窗口的盘中事件状态机。

不按固定买入时刻，也不按“最近N笔Tick”判断。不同流动性的股票都使用秒级窗口、
会话VWAP、盘口金额不平衡和自适应ATR阈值。只产生信号，不产生订单。
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, time as dt_time
from typing import Any, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

from t1_survivability import evaluate_t1_survivability


MARKET_TZ = ZoneInfo("Asia/Shanghai")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _tick_value(tick: Any, name: str, default: Any = None) -> Any:
    if isinstance(tick, dict):
        return tick.get(name, default)
    try:
        return getattr(tick, name)
    except (AttributeError, TypeError):
        try:
            return tick[name]
        except Exception:
            return default


def _parse_timestamp(value: Any, fallback: Optional[datetime] = None) -> datetime:
    fallback = fallback or datetime.now()
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            result = fallback
    if result.tzinfo is not None:
        try:
            result = result.astimezone(MARKET_TZ).replace(tzinfo=None)
        except Exception:
            result = result.replace(tzinfo=None)
    return result


def _normalize_quotes(raw_quotes: Any) -> List[Dict[str, float]]:
    quotes = raw_quotes or []
    result = []
    for index in range(5):
        raw = quotes[index] if index < len(quotes) else {}
        if not isinstance(raw, dict):
            try:
                raw = vars(raw)
            except Exception:
                raw = {}
        result.append({
            "level": index + 1,
            "bid_p": _safe_float(raw.get("bid_p", raw.get("bid_price", 0))),
            "bid_v": _safe_float(raw.get("bid_v", raw.get("bid_volume", 0))),
            "ask_p": _safe_float(raw.get("ask_p", raw.get("ask_price", 0))),
            "ask_v": _safe_float(raw.get("ask_v", raw.get("ask_volume", 0))),
        })
    return result


def normalize_tick(tick: Any, received_at: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    received_at = received_at or datetime.now()
    symbol = str(_tick_value(tick, "symbol", "") or "")
    price = _safe_float(_tick_value(tick, "price", 0.0))
    if not symbol or price <= 0:
        return None
    event_ts = _parse_timestamp(
        _tick_value(tick, "created_at", _tick_value(tick, "eob", None)),
        fallback=received_at,
    )
    cum_volume = _safe_float(_tick_value(tick, "cum_volume", _tick_value(tick, "volume", 0.0)))
    cum_amount = _safe_float(_tick_value(tick, "cum_amount", _tick_value(tick, "amount", 0.0)))
    quotes = _normalize_quotes(_tick_value(tick, "quotes", []))
    bid_amount = sum(row["bid_p"] * row["bid_v"] for row in quotes if row["bid_p"] > 0 and row["bid_v"] > 0)
    ask_amount = sum(row["ask_p"] * row["ask_v"] for row in quotes if row["ask_p"] > 0 and row["ask_v"] > 0)
    imbalance = (bid_amount - ask_amount) / (bid_amount + ask_amount) if bid_amount + ask_amount > 0 else None
    vwap = 0.0
    if cum_amount > 0 and cum_volume > 0:
        raw_vwap = cum_amount / cum_volume
        if 0.5 * price <= raw_vwap <= 1.5 * price:
            vwap = raw_vwap
        elif 0.5 * price <= raw_vwap / 100.0 <= 1.5 * price:
            vwap = raw_vwap / 100.0
    return {
        "symbol": symbol,
        "price": price,
        "event_ts": event_ts,
        "received_at": received_at,
        "cum_volume": cum_volume,
        "cum_amount": cum_amount,
        "vwap": vwap,
        "quotes": quotes,
        "bid1_price": quotes[0]["bid_p"],
        "ask1_price": quotes[0]["ask_p"],
        "top5_bid_amount": bid_amount,
        "top5_ask_amount": ask_amount,
        "amount_imbalance": imbalance,
        "timestamp_source": "exchange_created_at" if _tick_value(tick, "created_at", None) else "local_receive_time",
    }


def is_continuous_session(ts: datetime) -> bool:
    current = ts.time()
    return dt_time(9, 30) <= current <= dt_time(11, 30) or dt_time(13, 0) <= current <= dt_time(15, 0)


# 向后兼容旧研究脚本；实时服务统一调用公开函数，避免各引擎自行解释交易时段。
_is_continuous_session = is_continuous_session


class IntradayEventEngine:
    def __init__(self):
        self.states: Dict[str, Dict[str, Any]] = defaultdict(self._new_state)

    @staticmethod
    def _new_state() -> Dict[str, Any]:
        return {
            "trade_date": None,
            "samples": deque(maxlen=1800),
            "phase": "WAIT_IMPULSE",
            "impulse_high": 0.0,
            "impulse_at": None,
            "pullback_low": 0.0,
            "pullback_at": None,
            "session_high": 0.0,
            "session_low": float("inf"),
            "last_event": {},
            "buy_event_date": None,
            "opportunity_event_date": None,
            "opportunity_stage": 0,
            "opportunity_pattern": None,
            "trend_armed": False,
            "trend_armed_at": None,
            "trend_armed_price": 0.0,
            "trend_peak": 0.0,
            "trend_peak_at": None,
            "trend_pullback_low": 0.0,
            "trend_pullback_at": None,
            "trend_reclaim_at": None,
            "sudden_event_date": None,
            "sudden_event_level": 0,
            "sudden_discovered_at": None,
            "sudden_discovered_price": 0.0,
            "sudden_peak": 0.0,
            "sudden_pullback_low": 0.0,
            "sudden_pullback_at": None,
            "same_day_risk_candidate_at": None,
            "sell_candidate_at": None,
            "sell_candidate_key": None,
            "sell_emitted_tier": 0,
            "position_reduce_emitted_at": None,
            "position_recovery_candidate_at": None,
            "position_recovery_emitted": False,
        }

    @staticmethod
    def _window(samples: Deque[Dict[str, Any]], now: datetime, seconds: int) -> List[Dict[str, Any]]:
        # samples按时间递增；从末端反向扫描，越过窗口即可停止，避免每个Tick都遍历
        # 最多1800条历史样本。
        result = []
        for row in reversed(samples):
            elapsed = (now - row["event_ts"]).total_seconds()
            if elapsed < 0:
                continue
            if elapsed > seconds:
                break
            result.append(row)
        result.reverse()
        return result

    @staticmethod
    def _momentum(rows: List[Dict[str, Any]]) -> float:
        if len(rows) < 2 or rows[0]["price"] <= 0:
            return 0.0
        return rows[-1]["price"] / rows[0]["price"] - 1.0

    @staticmethod
    def _entry_quality(
        current: Dict[str, Any],
        vwap: float,
        momentum_short: float,
        momentum_long: float,
        live_sector: Dict[str, Any],
        auction_gate: Dict[str, Any],
        capital_behavior: Optional[Dict[str, Any]] = None,
    ) -> int:
        score = 0
        if vwap > 0:
            distance = abs(current["price"] / vwap - 1.0)
            score += 25 if distance <= 0.003 else (18 if distance <= 0.01 else (10 if distance <= 0.02 else 0))
        else:
            score += 8
        score += 10 if momentum_short > 0 else 0
        score += 10 if momentum_long > 0 else 0
        imbalance = current.get("amount_imbalance")
        score += 8 if imbalance is None else (20 if imbalance >= 0.15 else (12 if imbalance >= -0.10 else 0))
        score += {
            "EXPANSION": 25,
            "HEALTHY_TREND": 22,
            "IGNITION": 18,
            "DIVERGING": 10,
            "NEUTRAL": 8,
            "UNAVAILABLE": 5,
            "CONCENTRATED": 3,
            "DECAY": 0,
        }.get(str(live_sector.get("state") or "UNAVAILABLE"), 5)
        score += {"SUPPORT": 10, "NEUTRAL": 6, "CAUTION": 3}.get(str(auction_gate.get("gate") or "NEUTRAL"), 0)
        capital_behavior = capital_behavior or {}
        if capital_behavior.get("status") == "READY":
            capital_score = _safe_float(capital_behavior.get("score"), 50.0)
            score += int(round(_clip((capital_score - 50.0) * 0.20, -10.0, 10.0)))
            # 采用60秒持续证据替代单个盘口快照。瞬时卖盘偏大时，只要此前的主动成交、
            # OFI与VWAP接受度持续一致，就不应把一个抖动直接解释成买点失败。
            if capital_behavior.get("flow_persistence_confirmed"):
                score += 12
            if capital_behavior.get("phase") == "CONFIRMED_OUTFLOW":
                score -= 12
        return max(0, min(100, score))

    @staticmethod
    def _execution_quality(current: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(current.get("symbol") or "")
        code = symbol.split(".")[-1]
        limit_ratio = 0.20 if code.startswith(("300", "301", "688")) else 0.10
        reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
        intraday_return = current["price"] / reference_close - 1.0 if reference_close > 0 else 0.0
        near_limit = intraday_return >= limit_ratio - 0.006
        quotes = current.get("quotes") or []
        ask1 = _safe_float(current.get("ask1_price"))
        bid1 = _safe_float(current.get("bid1_price"))
        limit_locked = bool(near_limit and quotes and ask1 <= 0 and bid1 >= current["price"] * 0.999)
        executable = bool(not limit_locked and (ask1 > 0 or not quotes))
        return {
            "executable": executable,
            "limit_locked": limit_locked,
            "near_limit": near_limit,
            "intraday_return": intraday_return,
        }

    def _append_sample(self, state: Dict[str, Any], observation: Dict[str, Any]) -> bool:
        trade_date = observation["event_ts"].strftime("%Y-%m-%d")
        if state["trade_date"] != trade_date:
            state.clear()
            state.update(self._new_state())
            state["trade_date"] = trade_date
        samples: Deque[Dict[str, Any]] = state["samples"]
        if samples and observation["event_ts"] < samples[-1]["event_ts"]:
            return False
        if samples and (observation["event_ts"] - samples[-1]["event_ts"]).total_seconds() > 300:
            # 午间休市或数据中断后，旧的冲高/回撤形态已经失效；会话高低点仍保留给退出判断。
            state["phase"] = "WAIT_IMPULSE"
            state["impulse_high"] = 0.0
            state["impulse_at"] = None
            state["pullback_low"] = 0.0
            state["pullback_at"] = None
        # 同一秒只保留最新状态，避免高流动性股票在一秒内占满窗口。
        if samples and observation["event_ts"].replace(microsecond=0) == samples[-1]["event_ts"].replace(microsecond=0):
            samples[-1] = observation
        else:
            samples.append(observation)
        state["session_high"] = max(_safe_float(state.get("session_high")), observation["price"])
        state["session_low"] = min(_safe_float(state.get("session_low"), observation["price"]), observation["price"])
        return True

    def on_tick(
        self,
        tick: Any,
        candidate: Optional[Dict[str, Any]],
        auction_gate: Optional[Dict[str, Any]] = None,
        received_at: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        observation = normalize_tick(tick, received_at=received_at)
        if observation is None or not is_continuous_session(observation["event_ts"]):
            return None
        symbol = observation["symbol"]
        state = self.states[symbol]
        if not self._append_sample(state, observation):
            return None
        if not candidate:
            return None
        volume_factor = candidate.get("volume_soft_factor") or {}
        if candidate.get("monitor_sell"):
            entry_date = str(candidate.get("position_entry_date") or "")[:10]
            if entry_date == state.get("trade_date"):
                position_event = self._evaluate_same_day_continuation_risk(state, observation, candidate)
            else:
                position_event = self._evaluate_sell(state, observation, candidate)
            if position_event is not None:
                return position_event
            recovery_event = self._evaluate_position_recovery(state, observation, candidate)
            if recovery_event is not None:
                return recovery_event
            # 没有卖点时不能结束整条实时链路。昨日前底仓仍需继续评估新的回踩买点，
            # 该事件在行动层解释为继续持有/做T买入腿；不会自动下单。
            if candidate.get("action") == "EXIT":
                return None
        elif candidate.get("action") == "EXIT":
            return self._evaluate_sell(state, observation, candidate)
        sudden = candidate.get("sudden_trend_context") or {}
        if sudden.get("discovered") or state.get("sudden_event_date") == state.get("trade_date"):
            event = self._evaluate_sudden_trend(state, observation, candidate, auction_gate or {})
            if event is not None:
                return event
        intraday_eligible = bool(candidate.get("intraday_eligible")) or (
            candidate.get("action") == "BUY" and candidate.get("status") in {"A_PRIORITY", "A_TREND_PRIORITY"}
        )
        if not intraday_eligible:
            return None
        # 国内实时市场许可关闭时，只关闭新的T+1风险暴露；已有仓位的卖点、风险
        # 监控和持仓管理已经在上方先行处理。外围市场本身永远不能单独触发此门。
        if candidate.get("market_new_entry_allowed") is False:
            return None
        if volume_factor.get("blocks_new_entry"):
            return None
        if (auction_gate or {}).get("hard_veto") or (auction_gate or {}).get("gate") == "HARD_VETO":
            return None
        if state.get("buy_event_date") == state.get("trade_date"):
            return None
        return self._evaluate_buy(state, observation, candidate, auction_gate or {})

    def _evaluate_sudden_trend(
        self,
        state: Dict[str, Any],
        current: Dict[str, Any],
        candidate: Dict[str, Any],
        auction_gate: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """把原本不在D-1候选中的盘中突发趋势升级为独立事件。

        发现、可交易机会、正式T+1观察三者严格分级；涨停封死只允许进入发现层，
        不会被描述成可以买到的入场信号。
        """
        sudden = candidate.get("sudden_trend_context") or {}
        capital_behavior = candidate.get("capital_behavior") or {}
        if auction_gate.get("hard_veto") or auction_gate.get("gate") == "HARD_VETO":
            return None
        executable = bool(sudden.get("executable"))
        discovered_now = bool(sudden.get("discovered"))
        already_discovered = state.get("sudden_event_date") == state.get("trade_date")
        if not discovered_now and not already_discovered:
            return None
        price = current["price"]
        if discovered_now and state.get("sudden_discovered_at") is None:
            state["sudden_discovered_at"] = current["event_ts"]
            state["sudden_discovered_price"] = price
        if price >= _safe_float(state.get("sudden_peak")):
            state["sudden_peak"] = price
        peak = _safe_float(state.get("sudden_peak"), price)
        atr = min(max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015), 0.15)
        pullback_min = min(max(0.08 * atr, 0.004), 0.010)
        pullback_max = min(max(0.35 * atr, 0.015), 0.035)
        retrace = (peak - price) / peak if peak > 0 else 0.0
        vwap_gap = sudden.get("vwap_gap")
        vwap_gap_cap = min(max(0.18 * atr, 0.008), 0.015)
        if pullback_min <= retrace <= pullback_max:
            if state.get("sudden_pullback_at") is None:
                state["sudden_pullback_at"] = current["event_ts"]
                state["sudden_pullback_low"] = price
            else:
                state["sudden_pullback_low"] = min(
                    _safe_float(state.get("sudden_pullback_low"), price), price
                )
        pullback_low = _safe_float(state.get("sudden_pullback_low"))
        reclaim_level = (
            pullback_low + 0.40 * (peak - pullback_low)
            if pullback_low > 0 and peak > pullback_low else 0.0
        )
        pullback_age = (
            (current["event_ts"] - state["sudden_pullback_at"]).total_seconds()
            if state.get("sudden_pullback_at") is not None else 0.0
        )
        momentum_30 = self._momentum(self._window(state["samples"], current["event_ts"], 30))
        pullback_reclaimed = bool(
            pullback_age >= 20
            and reclaim_level > 0
            and price >= reclaim_level
            and momentum_30 > 0
            and (vwap_gap is None or _safe_float(vwap_gap) <= vwap_gap_cap)
        )
        discovered_at = state.get("sudden_discovered_at")
        signal_age_minutes = (
            (current["event_ts"] - discovered_at).total_seconds() / 60.0
            if discovered_at is not None else 0.0
        )
        t1_survivability = evaluate_t1_survivability(
            price=price,
            session_open=_safe_float(state["samples"][0].get("price"), price),
            session_high=_safe_float(state.get("session_high"), price),
            session_low=_safe_float(state.get("session_low"), price),
            vwap=current.get("vwap"),
            reference_close=sudden.get("reference_close"),
            atr14_pct=_safe_float(candidate.get("atr14_pct"), 0.05),
            momentum_60s=self._momentum(self._window(state["samples"], current["event_ts"], 60)),
            momentum_180s=self._momentum(self._window(state["samples"], current["event_ts"], 180)),
            sector=candidate.get("continuation_sector") or candidate.get("live_sector") or {},
            continuation=candidate.get("continuation_context") or {},
            capital_behavior=capital_behavior,
            multitimeframe=candidate.get("multitimeframe") or {},
            route="SUDDEN_TREND",
            signal_age_minutes=signal_age_minutes,
            discovery_price=_safe_float(state.get("sudden_discovered_price"), price),
        )
        formal = bool(
            sudden.get("formal_t1_entry")
            and candidate.get("market_new_entry_allowed") is not False
            and executable
            and pullback_reclaimed
            and t1_survivability.get("formal_entry_allowed")
            and (
                capital_behavior.get("status") != "READY"
                or (
                    capital_behavior.get("entry_support")
                    and capital_behavior.get("phase") != "CONFIRMED_OUTFLOW"
                )
            )
        )
        if formal and state.get("buy_event_date") == state.get("trade_date"):
            return None
        if formal:
            event_type, event_kind, grade, pattern = (
                "BUY_EVENT_WATCH", "BUY", "T1_NEW_ENTRY", "SUDDEN_TREND_BREAKOUT",
            )
        elif not executable:
            event_type, event_kind, grade, pattern = (
                "DISCOVERY_EVENT_WATCH", "DISCOVERY", "UNEXECUTABLE_DISCOVERY", "SUDDEN_TREND_LIMIT_LOCKED",
            )
        else:
            event_type, event_kind, grade, pattern = (
                "OPPORTUNITY_EVENT_WATCH", "OPPORTUNITY", "SUDDEN_TREND_WATCH", "SUDDEN_TREND_ARMED",
            )
        event_level = {"DISCOVERY": 1, "OPPORTUNITY": 2, "BUY": 3}[event_kind]
        if (
            state.get("sudden_event_date") == state.get("trade_date")
            and int(_safe_float(state.get("sudden_event_level"))) >= event_level
        ):
            return None
        event_id = f"{state['trade_date']}:{current['symbol']}:{event_kind}_{pattern}"
        state["last_event"][event_kind] = event_id
        state["sudden_event_date"] = state["trade_date"]
        state["sudden_event_level"] = event_level
        if formal:
            state["buy_event_date"] = state["trade_date"]
            state["phase"] = "SIGNALLED"
        elif executable:
            state["opportunity_event_date"] = state["trade_date"]
        return {
            "event": event_type,
            "pattern": pattern,
            "signal_grade": grade,
            "event_id": event_id,
            "symbol": current["symbol"],
            "event_ts": current["event_ts"].isoformat(),
            "price": current["price"],
            "vwap": current.get("vwap") or None,
            "vwap_gap": sudden.get("vwap_gap"),
            "entry_vwap_gap_cap": vwap_gap_cap,
            "entry_state": "ENTRY_READY_AFTER_RECLAIM" if formal else "ARMED_WAIT_PULLBACK",
            "entry_tier": "B" if formal else "WATCH",
            "trend_peak": peak,
            "trend_retrace": retrace,
            "trend_pullback_low": pullback_low or None,
            "trend_reclaim_level": reclaim_level or None,
            "recent_pullback_reclaimed": pullback_reclaimed,
            "signal_age_minutes": signal_age_minutes,
            "t1_survivability": t1_survivability,
            "reference_close": sudden.get("reference_close"),
            "intraday_return": sudden.get("intraday_return"),
            "amount_imbalance": current.get("amount_imbalance"),
            "daily_route": candidate.get("daily_route"),
            "live_signal_strength": sudden.get("score"),
            "entry_quality": sudden.get("entry_quality"),
            "composite_signal_strength": sudden.get("score"),
            "live_sector": candidate.get("live_sector") or {},
            "multitimeframe": candidate.get("multitimeframe") or {},
            "capital_behavior": capital_behavior,
            "sudden_trend": sudden,
            "auction_gate": auction_gate.get("gate", "NEUTRAL"),
            "auction_label": auction_gate.get("label", "NO_AUCTION_EVIDENCE"),
            "executable": executable,
            "limit_locked": bool(sudden.get("limit_locked")),
            "order_submitted": False,
        }

    def _evaluate_buy(
        self,
        state: Dict[str, Any],
        current: Dict[str, Any],
        candidate: Dict[str, Any],
        auction_gate: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        samples = state["samples"]
        now = current["event_ts"]
        window_180 = self._window(samples, now, 180)
        if len(window_180) < 3 or (window_180[-1]["event_ts"] - window_180[0]["event_ts"]).total_seconds() < 120:
            return None
        atr = min(max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015), 0.15)
        impulse_threshold = min(max(0.08 * atr, 0.0025), 0.008)
        pullback_min = min(max(0.05 * atr, 0.0020), 0.006)
        pullback_max = min(max(0.30 * atr, 0.0080), 0.025)
        vwap = _safe_float(current.get("vwap"))
        vwap_valid = vwap > 0
        imbalance = current.get("amount_imbalance")
        microstructure_available = vwap_valid or imbalance is not None
        above_vwap = not vwap_valid or current["price"] >= vwap * 0.999
        route = str(candidate.get("daily_route") or candidate.get("lane") or "")
        live_sector = candidate.get("live_sector") or {}
        market_sector = candidate.get("market_sector") or {}
        continuation = candidate.get("continuation_context") or {}
        multitimeframe = candidate.get("multitimeframe") or {}
        capital_behavior = candidate.get("capital_behavior") or {}
        sector_state = str(live_sector.get("state") or "UNAVAILABLE")
        sector_role = str(live_sector.get("role") or "UNCLASSIFIED")
        strength = int(_safe_float(candidate.get("live_signal_strength"), _safe_float(candidate.get("signal_strength"))))
        momentum_60 = self._momentum(self._window(samples, now, 60))
        momentum_180 = self._momentum(window_180)
        order_ok = imbalance is None or imbalance >= -0.10
        vwap_extension_cap = min(max(0.40 * atr, 0.015), 0.05)
        price_not_far_from_vwap = not vwap_valid or current["price"] <= vwap * (1.0 + vwap_extension_cap)
        execution = self._execution_quality(current, candidate)
        mtf_formal_ready = bool(
            not multitimeframe.get("periods")
            or (
                multitimeframe.get("alignment") in {"FULL_BULLISH", "BULLISH_2_OF_3"}
                and _safe_float(multitimeframe.get("score")) >= 70
            )
        )

        if not microstructure_available:
            return None
        # 新版实盘候选必须等待5分钟触发，并至少得到15/30分钟之一支持。
        # 旧数据或单元测试没有多周期上下文时保留兼容降级，不伪造“已确认”。
        periods = multitimeframe.get("periods") or {}
        five_minute = periods.get("5") or {}
        fifteen_minute = periods.get("15") or {}
        thirty_minute = periods.get("30") or {}
        sixty_minute = periods.get("60") or {}
        def _confirmed_bearish_divergence(period: Dict[str, Any]) -> bool:
            return bool(
                period.get("macd_divergence") == "BEARISH"
                and period.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
                and int(_safe_float(period.get("divergence_quality"))) >= 55
            )
        # 板块前排的替代多周期通道：用户约定30/60分钟是顶底背离的主风险周期，
        # 因而15分钟历史背离只作谨慎项，不能在5分钟价格/MACD未弱、15分钟KDJ与
        # MACD正在改善且30/60分钟均无有效顶背离时一票否决健康续强。
        leader_mtf_structure_precheck = bool(
            route == "TREND_CONTINUATION"
            and periods
            and five_minute.get("state") != "BEARISH"
            and five_minute.get("macd_bullish")
            and _safe_float(five_minute.get("close")) >= _safe_float(five_minute.get("ma20"), float("inf"))
            and fifteen_minute.get("state") != "BEARISH"
            and fifteen_minute.get("kdj_bullish")
            and (fifteen_minute.get("macd_improving") or fifteen_minute.get("macd_bullish"))
            and not _confirmed_bearish_divergence(thirty_minute)
            and not _confirmed_bearish_divergence(sixty_minute)
        )
        structure = capital_behavior.get("structure") or candidate.get("capital_structure") or {}
        early_structure_ready = bool(
            structure.get("phase") in {"ACCUMULATION", "BALANCED"}
            or (
                structure.get("phase") == "REACCUMULATION"
                and _safe_float(structure.get("range_position60"), 1.0) <= 0.65
            )
        )
        capital_led_early_mtf_ready = bool(
            multitimeframe.get("periods")
            and capital_behavior.get("flow_persistence_confirmed")
            and early_structure_ready
            and multitimeframe.get("recent_five_minute_trigger")
            and five_minute.get("supportive")
            and _safe_float(five_minute.get("score")) >= 80
            and int(_safe_float(multitimeframe.get("bearish_count"))) <= 1
            and multitimeframe.get("alignment") != "BEARISH_2_OF_3"
        )
        if (
            multitimeframe.get("periods")
            and not multitimeframe.get("trigger_confirmed")
            and not capital_led_early_mtf_ready
            and not leader_mtf_structure_precheck
        ):
            return None
        if sector_state == "DECAY" or (sector_state == "CONCENTRATED" and sector_role == "LAGGARD"):
            return None
        market_state = str(market_sector.get("rotation_state") or "UNAVAILABLE")
        compensating_local_strength = bool(
            sector_state in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
            and sector_role in {"LEADER", "FRONT", "CORE"}
            and _safe_float(market_sector.get("health_percentile")) >= 0.80
            and market_state not in {"FLASH_HEAT", "ROTATION_OUT", "WEAK"}
        )
        if market_state in {"ROTATION_OUT", "WEAK"} or (
            market_sector.get("rotation_caution") and not compensating_local_strength
        ) or (
            market_sector.get("board_code")
            and not market_sector.get("entry_support")
            and not compensating_local_strength
        ):
            return None

        # 趋势路线先进入“已武装”状态，再等待近期、可验证的回踩—收复。
        # 2026-08-11盘后审计发现，旧逻辑会把几十分钟前的承接证据用于当前冲高，
        # 4个正式BUY全部在离VWAP 1.14%~2.91%的位置触发并收跌。因此正式新仓不再
        # 由“持续上涨+高分”直接产生；趋势很强但位置不合适时继续监控，而不是静默丢弃。
        continuation_confirmed = bool(continuation.get("confirmed"))
        continuation_status = str(continuation.get("status") or "")
        trend_context_valid = bool(
            route == "TREND_CONTINUATION"
            and strength >= 70
            and continuation_status not in {"DATA_CONFLICT", "LIMIT_LOCKED", "FAILED_ACCEPTANCE"}
            and (continuation_confirmed or state.get("trend_armed"))
        )
        if trend_context_valid:
            reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
            entry_quality = self._entry_quality(
                current, vwap, momentum_60, momentum_180, live_sector, auction_gate, capital_behavior,
            )
            composite_strength = int(round(0.60 * strength + 0.40 * entry_quality))
            if entry_quality < 55 or composite_strength < 72:
                return None

            state["trend_armed"] = True
            if state.get("trend_armed_at") is None:
                state["trend_armed_at"] = now
                state["trend_armed_price"] = current["price"]
            if current["price"] >= _safe_float(state.get("trend_peak")):
                state["trend_peak"] = current["price"]
                state["trend_peak_at"] = now
            trend_peak = _safe_float(state.get("trend_peak"), current["price"])
            trend_retrace = (trend_peak - current["price"]) / trend_peak if trend_peak > 0 else 0.0
            trend_pullback_min = min(max(0.08 * atr, 0.003), 0.008)
            trend_pullback_max = min(max(0.35 * atr, 0.012), 0.030)
            entry_vwap_gap_cap = min(max(0.16 * atr, 0.006), 0.012)
            # 持续资金流通道允许比回踩买点略宽的位置，但仍限制在VWAP附近。
            # 这不是放开追高：上限仅1.6%，且还要求60秒持续证据、板块和多周期共振。
            flow_vwap_gap_cap = min(max(0.22 * atr, 0.010), 0.016)
            vwap_gap = current["price"] / vwap - 1.0 if vwap > 0 else None

            if (
                trend_pullback_min <= trend_retrace <= trend_pullback_max
                and (not vwap_valid or current["price"] >= vwap * 0.995)
            ):
                if state.get("trend_pullback_at") is None:
                    state["trend_pullback_at"] = now
                    state["trend_pullback_low"] = current["price"]
                else:
                    state["trend_pullback_low"] = min(
                        _safe_float(state.get("trend_pullback_low"), current["price"]), current["price"]
                    )
                state["phase"] = "TREND_PULLBACK"
            elif state.get("phase") == "TREND_PULLBACK" and (
                trend_retrace > trend_pullback_max
                or (vwap_valid and current["price"] < vwap * 0.992)
            ):
                # 深回撤或跌破承接区后，本轮回踩失效；保留趋势观察资格，等待新结构。
                state["phase"] = "TREND_ARMED"
                state["trend_pullback_low"] = 0.0
                state["trend_pullback_at"] = None
                state["trend_reclaim_at"] = None

            pullback_low = _safe_float(state.get("trend_pullback_low"))
            reclaim_level = (
                pullback_low + 0.40 * (trend_peak - pullback_low)
                if pullback_low > 0 and trend_peak > pullback_low else 0.0
            )
            pullback_age = (
                (now - state["trend_pullback_at"]).total_seconds()
                if state.get("trend_pullback_at") is not None else 0.0
            )
            momentum_30 = self._momentum(self._window(samples, now, 30))
            recent_pullback_reclaimed = bool(
                state.get("phase") == "TREND_PULLBACK"
                and pullback_age >= 20
                and reclaim_level > 0
                and current["price"] >= reclaim_level
                and above_vwap
                and momentum_30 > 0
                and order_ok
                and (vwap_gap is None or vwap_gap <= entry_vwap_gap_cap)
            )
            if recent_pullback_reclaimed and state.get("trend_reclaim_at") is None:
                state["trend_reclaim_at"] = now

            continuation_daily = _safe_float((continuation.get("daily") or {}).get("score"))
            continuation_auction = _safe_float((continuation.get("auction") or {}).get("score"))
            continuation_sector = _safe_float((continuation.get("sector") or {}).get("score"))
            continuation_acceptance = _safe_float(continuation.get("acceptance_score"))
            auction_ready = bool(
                continuation_auction >= 55
                or continuation.get("auction_repaired_by_live_acceptance")
            )
            sector_ready = bool(
                continuation_sector >= 65
                or (
                    sector_state in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
                    and sector_role in {"LEADER", "FRONT", "CORE", "FOLLOWER"}
                )
            )
            mtf_periods_ready = bool(multitimeframe.get("periods"))
            mtf_soft_ready = bool(
                capital_led_early_mtf_ready
                or leader_mtf_structure_precheck
                or (
                    mtf_periods_ready
                    and multitimeframe.get("trigger_confirmed")
                    and _safe_float(multitimeframe.get("score")) >= 70
                    and int(_safe_float(multitimeframe.get("bearish_count"))) <= 1
                    and multitimeframe.get("alignment") != "BEARISH_2_OF_3"
                )
            )
            capital_available = capital_behavior.get("status") == "READY"
            capital_entry_ready = bool(
                not capital_available
                or (
                    capital_behavior.get("entry_support")
                    and capital_behavior.get("phase") != "CONFIRMED_OUTFLOW"
                    and _safe_float(capital_behavior.get("score")) >= 62
                )
            )
            # 除“回踩—收复”外，允许一条严格的持续流入通道：必须已经观察至少90秒、
            # 价格仍贴近VWAP、大周期资金结构与板块同向、分钟周期确认且订单流持续。
            # 这用于识别不深回踩的健康主升，不等同于看见上涨就追。
            flow_continuation_ready = bool(
                capital_available
                and (
                    capital_behavior.get("continuation_acceleration")
                    or capital_led_early_mtf_ready
                )
                and early_structure_ready
                and momentum_60 > 0
                and (vwap_gap is None or vwap_gap <= flow_vwap_gap_cap)
                and state.get("trend_armed_at") is not None
                # 早期通道自身已经要求60秒持续资金证据和一根完成的5分钟触发，
                # 不再重复等待90秒；完整周期通道仍保留武装观察期。
                and (
                    capital_led_early_mtf_ready
                    or (now - state["trend_armed_at"]).total_seconds() >= 90
                )
            )
            # 板块动量策略的“前排接受式续强”通道。它只服务于已经被全市场板块、
            # 5/15分钟和持续资金共同证明的前排股，解决健康主升因不属于底部/再积累
            # 结构而被统一要求二次回踩的问题。门槛比普通持续流入更严格，且主板
            # +3%/20%板+6%以后关闭，避免把尾段加速当早期买点。
            code = str(current.get("symbol") or "").split(".")[-1]
            limit_ratio = 0.20 if code.startswith(("300", "301", "688")) else 0.10
            intraday_return = current["price"] / reference_close - 1.0 if reference_close > 0 else 0.0
            five_period = periods.get("5") or {}
            fifteen_period = periods.get("15") or {}
            divergence_30_60 = str(multitimeframe.get("divergence_30_60") or "NONE")
            strict_leader_mtf_ready = bool(
                multitimeframe.get("trigger_confirmed")
                and _safe_float(multitimeframe.get("score")) >= 74
                and int(_safe_float(multitimeframe.get("bearish_count"))) <= 1
                and five_period.get("supportive") and fifteen_period.get("supportive")
                and not divergence_30_60.startswith("BEARISH")
            )
            leader_capital_ready = bool(
                (
                    capital_behavior.get("phase") in {"CONTROLLED_ADVANCE", "AGGRESSIVE_INFLOW"}
                    and _safe_float(capital_behavior.get("score")) >= 72
                    and capital_behavior.get("flow_persistence_confirmed")
                    and capital_behavior.get("entry_support")
                )
                or (
                    capital_behavior.get("phase") == "SELLING_ABSORBED"
                    and _safe_float(capital_behavior.get("score")) >= 65
                    and capital_behavior.get("entry_support")
                    and capital_behavior.get("hold_support")
                )
            )
            leader_acceptance_return_cap = min(0.06, limit_ratio * 0.30)
            leader_acceptance_ready = bool(
                continuation_confirmed
                and _safe_float(continuation.get("score")) >= 75
                and _safe_float(continuation.get("acceptance_score")) >= 82
                and _safe_float(continuation.get("observation_seconds")) >= 150
                and _safe_float(continuation.get("confirmation_persistence_seconds")) >= 45
                and continuation.get("support_tested") and continuation.get("support_reclaimed")
                and sector_role in {"LEADER", "FRONT", "CORE"}
                and continuation_sector >= 68
                and market_sector.get("entry_support")
                and not market_sector.get("rotation_caution")
                and (strict_leader_mtf_ready or leader_mtf_structure_precheck)
                and leader_capital_ready
                and momentum_60 > 0
                and (vwap_gap is None or vwap_gap <= flow_vwap_gap_cap)
                and intraday_return <= leader_acceptance_return_cap
            )
            # 留出涨停前的可成交与T+1隔夜风险空间。主板约+7.8%、20%板约+15.6%以后，
            # 仍可发趋势机会，但不把持续流入升级成正式新仓。
            new_entry_extension_ok = intraday_return <= limit_ratio * 0.78
            signal_age_minutes = (
                (now - state["trend_armed_at"]).total_seconds() / 60.0
                if state.get("trend_armed_at") is not None else 0.0
            )
            continuation_sector = dict(live_sector)
            continuation_sector.update({
                "market_board_state": market_sector.get("rotation_state"),
                "market_board_entry_support": market_sector.get("entry_support"),
                "market_board_rotation_caution": market_sector.get("rotation_caution"),
            })
            t1_survivability = evaluate_t1_survivability(
                price=current["price"],
                session_open=_safe_float(samples[0].get("price"), current["price"]),
                session_high=_safe_float(state.get("session_high"), current["price"]),
                session_low=_safe_float(state.get("session_low"), current["price"]),
                vwap=vwap or None,
                reference_close=reference_close or None,
                atr14_pct=atr,
                momentum_60s=momentum_60,
                momentum_180s=momentum_180,
                sector=continuation_sector,
                continuation=continuation,
                capital_behavior=capital_behavior,
                multitimeframe=multitimeframe,
                route=route,
                signal_age_minutes=signal_age_minutes,
                discovery_price=_safe_float(state.get("trend_armed_price"), current["price"]),
            )
            strength_floor = 77 if capital_led_early_mtf_ready else 80
            quality_floor = 70 if capital_led_early_mtf_ready else 70
            t1_entry_ready = bool(
                candidate.get("market_new_entry_allowed") is not False
                and
                composite_strength >= strength_floor
                and entry_quality >= quality_floor
                and continuation_daily >= 65
                and auction_ready
                and sector_ready
                and continuation_acceptance >= 78
                and sector_role != "LAGGARD"
                and mtf_soft_ready
                and continuation_confirmed
                and capital_entry_ready
                and (recent_pullback_reclaimed or flow_continuation_ready or leader_acceptance_ready)
                and execution["executable"]
                and new_entry_extension_ok
                and t1_survivability.get("formal_entry_allowed")
            )
            platform_rows = self._window(samples, now, 300)
            platform_prior = platform_rows[:-1]
            platform_span = (
                (platform_prior[-1]["event_ts"] - platform_prior[0]["event_ts"]).total_seconds()
                if len(platform_prior) >= 2 else 0.0
            )
            platform_prices = [_safe_float(row.get("price")) for row in platform_prior]
            platform_mid = sorted(platform_prices)[len(platform_prices) // 2] if platform_prices else 0.0
            platform_range = (
                (max(platform_prices) - min(platform_prices)) / platform_mid
                if platform_mid > 0 and platform_prices else None
            )
            platform_range_cap = min(max(0.28 * atr, 0.008), 0.018)
            platform_reacceleration_shadow = bool(
                not t1_entry_ready
                and t1_survivability.get("grade") == "A"
                and platform_span >= 240
                and platform_range is not None
                and platform_range <= platform_range_cap
                and current["price"] >= max(platform_prices) * 1.0005
                and momentum_60 >= 0.002
                and (vwap_gap is None or vwap_gap <= 0.020)
                and mtf_soft_ready
                and capital_behavior.get("flow_persistence_confirmed")
                and sector_ready
                and execution["executable"]
                and new_entry_extension_ok
            )
            if execution["limit_locked"]:
                event_kind, event_type, pattern, opportunity_stage = (
                    "DISCOVERY", "DISCOVERY_EVENT_WATCH", "TREND_LIMIT_LOCKED", 0,
                )
                entry_state = "UNEXECUTABLE"
            elif t1_entry_ready:
                event_kind, event_type, pattern, opportunity_stage = (
                    "BUY", "BUY_EVENT_WATCH",
                    "CAPITAL_FLOW_CONTINUATION"
                    if flow_continuation_ready and not recent_pullback_reclaimed and not capital_led_early_mtf_ready
                    else "SECTOR_LEADER_ACCEPTANCE"
                    if leader_acceptance_ready and not recent_pullback_reclaimed
                    else "CAPITAL_LED_EARLY_REVERSAL"
                    if flow_continuation_ready and capital_led_early_mtf_ready
                    else "TREND_PULLBACK_RECLAIM",
                    3,
                )
                entry_state = (
                    "ENTRY_READY_CAPITAL_LED_EARLY"
                    if flow_continuation_ready and capital_led_early_mtf_ready
                    else "ENTRY_READY_SECTOR_LEADER_ACCEPTANCE"
                    if leader_acceptance_ready and not recent_pullback_reclaimed
                    else "ENTRY_READY_FLOW_CONTINUATION"
                    if flow_continuation_ready and not recent_pullback_reclaimed
                    else "ENTRY_READY_AFTER_RECLAIM"
                )
            else:
                event_kind, event_type = "OPPORTUNITY", "OPPORTUNITY_EVENT_WATCH"
                if platform_reacceleration_shadow:
                    pattern, opportunity_stage, entry_state = (
                        "PLATFORM_REACCELERATION_SHADOW", 4, "PLATFORM_REACCELERATION_SHADOW",
                    )
                elif not mtf_periods_ready:
                    pattern, opportunity_stage, entry_state = (
                        "PRELIMINARY_TREND_WATCH", 1, "WAIT_COMPLETED_5M_TRIGGER",
                    )
                elif state.get("phase") == "TREND_PULLBACK":
                    pattern, opportunity_stage, entry_state = (
                        "PULLBACK_IN_PROGRESS", 3, "WAIT_RECLAIM_CONFIRMATION",
                    )
                else:
                    pattern, opportunity_stage, entry_state = (
                        "ARMED_WAIT_PULLBACK", 2, "ARMED_WAIT_PULLBACK",
                    )
            event_id = f"{state['trade_date']}:{current['symbol']}:{event_kind}_{pattern}"
            if event_kind == "OPPORTUNITY":
                same_day = state.get("opportunity_event_date") == state.get("trade_date")
                old_stage = int(_safe_float(state.get("opportunity_stage")))
                if same_day and opportunity_stage <= old_stage:
                    return None
            if state["last_event"].get(event_kind) != event_id:
                state["last_event"][event_kind] = event_id
                if t1_entry_ready:
                    state["buy_event_date"] = state["trade_date"]
                    state["phase"] = "SIGNALLED"
                else:
                    state["opportunity_event_date"] = state["trade_date"]
                    state["opportunity_stage"] = opportunity_stage
                    state["opportunity_pattern"] = pattern
                return {
                    "event": event_type,
                    "pattern": pattern,
                    "signal_grade": (
                        "UNEXECUTABLE_DISCOVERY" if execution["limit_locked"]
                        else ("T1_NEW_ENTRY" if t1_entry_ready else "TREND_WATCH_OR_EXISTING_POSITION_T")
                    ),
                    "event_id": event_id,
                    "symbol": current["symbol"],
                    "event_ts": now.isoformat(),
                    "price": current["price"],
                    "vwap": vwap or None,
                    "vwap_gap": vwap_gap,
                    "entry_vwap_gap_cap": entry_vwap_gap_cap,
                    "flow_entry_vwap_gap_cap": flow_vwap_gap_cap,
                    "sector_leader_acceptance_return_cap": leader_acceptance_return_cap,
                    "sector_leader_acceptance_max_price": (
                        reference_close * (1.0 + leader_acceptance_return_cap) if reference_close > 0 else None
                    ),
                    "entry_state": entry_state,
                    "entry_tier": t1_survivability.get("grade") if t1_entry_ready else "WATCH",
                    "confirmation_tier": (
                        "A" if t1_entry_ready and int(_safe_float(multitimeframe.get("bearish_count"))) == 0
                        else ("B" if t1_entry_ready else "WATCH")
                    ),
                    "trend_peak": trend_peak,
                    "trend_retrace": trend_retrace,
                    "trend_pullback_low": pullback_low or None,
                    "trend_reclaim_level": reclaim_level or None,
                    "recent_pullback_reclaimed": recent_pullback_reclaimed,
                    "signal_age_minutes": signal_age_minutes,
                    "t1_survivability": t1_survivability,
                    "platform_shadow": {
                        "active": platform_reacceleration_shadow,
                        "window_seconds": platform_span,
                        "range": platform_range,
                        "range_cap": platform_range_cap,
                        "research_status": "SHADOW_ONLY_NOT_FORMAL_ENTRY",
                    },
                    "reference_close": reference_close or None,
                    "intraday_return": current["price"] / reference_close - 1.0 if reference_close > 0 else None,
                    "amount_imbalance": imbalance,
                    "momentum_60s": momentum_60,
                    "momentum_180s": momentum_180,
                    "daily_route": route,
                    "live_signal_strength": strength,
                    "entry_quality": entry_quality,
                    "composite_signal_strength": composite_strength,
                    "live_sector": live_sector,
                    "continuation": continuation,
                    "multitimeframe": multitimeframe,
                    "capital_behavior": capital_behavior,
                    "gate_evidence": {
                        "auction_ready": auction_ready,
                        "sector_ready": sector_ready,
                        "multitimeframe_ready": mtf_soft_ready,
                        "recent_pullback_reclaimed": recent_pullback_reclaimed,
                        "capital_entry_ready": capital_entry_ready,
                        "capital_flow_continuation_ready": flow_continuation_ready,
                        "capital_led_early_mtf_ready": capital_led_early_mtf_ready,
                        "sector_leader_acceptance_ready": leader_acceptance_ready,
                        "sector_leader_mtf_structure_precheck": leader_mtf_structure_precheck,
                        "sector_leader_capital_ready": leader_capital_ready,
                        "sector_leader_acceptance_return_cap": leader_acceptance_return_cap,
                        "early_structure_ready": early_structure_ready,
                        "location_ready": vwap_gap is None or vwap_gap <= entry_vwap_gap_cap,
                        "flow_location_ready": vwap_gap is None or vwap_gap <= flow_vwap_gap_cap,
                        "new_entry_extension_ok": new_entry_extension_ok,
                        "t1_survivability_ready": t1_survivability.get("formal_entry_allowed"),
                    },
                    "executable": execution["executable"],
                    "limit_locked": execution["limit_locked"],
                    "data_quality": "VWAP_AND_OR_TOP5_AMOUNT_AVAILABLE",
                    "auction_gate": auction_gate.get("gate", "NEUTRAL"),
                    "auction_label": auction_gate.get("label", "NO_AUCTION_EVIDENCE"),
                    "order_submitted": False,
                }

        # 趋势延续路线不能从通用状态机旁路产生买点；它只能沿上面的
        # “预观察→已武装→回踩中→收复可执行”生命周期前进。
        if route == "TREND_CONTINUATION":
            return None

        if state["phase"] == "WAIT_IMPULSE":
            base = min(row["price"] for row in window_180)
            if base > 0 and current["price"] >= base * (1.0 + impulse_threshold) and above_vwap and momentum_60 > 0:
                state["phase"] = "IMPULSE"
                state["impulse_high"] = current["price"]
                state["impulse_at"] = now
            return None

        if state["phase"] == "IMPULSE":
            state["impulse_high"] = max(_safe_float(state.get("impulse_high")), current["price"])
            retrace = (state["impulse_high"] - current["price"]) / state["impulse_high"] if state["impulse_high"] > 0 else 0.0
            if retrace > pullback_max or (vwap_valid and current["price"] < vwap * 0.992):
                state["phase"] = "WAIT_IMPULSE"
                return None
            impulse_age = (now - state["impulse_at"]).total_seconds() if state.get("impulse_at") else 0
            if pullback_min <= retrace <= pullback_max and impulse_age >= 10 and above_vwap:
                state["phase"] = "PULLBACK"
                state["pullback_low"] = current["price"]
                state["pullback_at"] = now
            return None

        if state["phase"] == "PULLBACK":
            state["pullback_low"] = min(_safe_float(state.get("pullback_low"), current["price"]), current["price"])
            total_retrace = (state["impulse_high"] - current["price"]) / state["impulse_high"] if state["impulse_high"] > 0 else 0.0
            if total_retrace > pullback_max or (vwap_valid and current["price"] < vwap * 0.992):
                state["phase"] = "WAIT_IMPULSE"
                return None
            recovery_level = state["pullback_low"] + 0.50 * (state["impulse_high"] - state["pullback_low"])
            momentum_30 = self._momentum(self._window(samples, now, 30))
            pullback_age = (now - state["pullback_at"]).total_seconds() if state.get("pullback_at") else 0
            reference_close = _safe_float(candidate.get("close"), _safe_float(candidate.get("pre_close")))
            # 不再用固定“高于昨收5%”否决；改为判断相对当日VWAP是否已经偏离过远。
            # reference_close仍记录在事件里，供盘后复盘真实涨幅。
            not_intraday_chasing = price_not_far_from_vwap
            confirmed = (
                pullback_age >= 10
                and current["price"] >= recovery_level
                and above_vwap
                and momentum_30 > 0
                and order_ok
                and not_intraday_chasing
            )
            if confirmed:
                momentum_180 = self._momentum(window_180)
                entry_quality = self._entry_quality(
                    current, vwap, momentum_30, momentum_180, live_sector, auction_gate, capital_behavior,
                )
                composite_strength = int(round(0.60 * strength + 0.40 * entry_quality))
                generic_signal_age_minutes = (
                    (now - state["impulse_at"]).total_seconds() / 60.0
                    if state.get("impulse_at") is not None else 0.0
                )
                t1_survivability = evaluate_t1_survivability(
                    price=current["price"],
                    session_open=_safe_float(samples[0].get("price"), current["price"]),
                    session_high=_safe_float(state.get("session_high"), current["price"]),
                    session_low=_safe_float(state.get("session_low"), current["price"]),
                    vwap=vwap or None,
                    reference_close=reference_close or None,
                    atr14_pct=atr,
                    momentum_60s=momentum_30,
                    momentum_180s=momentum_180,
                    sector=candidate.get("continuation_sector") or live_sector,
                    continuation=candidate.get("continuation_context") or {},
                    capital_behavior=capital_behavior,
                    multitimeframe=multitimeframe,
                    route=route or "GENERIC_PULLBACK",
                    signal_age_minutes=generic_signal_age_minutes,
                    discovery_price=_safe_float(state.get("impulse_high"), current["price"]),
                )
                t1_entry_ready = bool(
                    candidate.get("market_new_entry_allowed") is not False
                    and
                    composite_strength >= 80
                    and entry_quality >= 70
                    and sector_state in {"IGNITION", "EXPANSION", "HEALTHY_TREND"}
                    and sector_role != "LAGGARD"
                    and mtf_formal_ready
                    and (
                        capital_behavior.get("status") != "READY"
                        or (
                            capital_behavior.get("entry_support")
                            and capital_behavior.get("phase") != "CONFIRMED_OUTFLOW"
                        )
                    )
                    and execution["executable"]
                    and t1_survivability.get("formal_entry_allowed")
                )
                if execution["limit_locked"]:
                    event_kind, event_type, pattern = "DISCOVERY", "DISCOVERY_EVENT_WATCH", "TREND_LIMIT_LOCKED"
                else:
                    event_kind = "BUY" if t1_entry_ready else "OPPORTUNITY"
                    event_type = "BUY_EVENT_WATCH" if t1_entry_ready else "OPPORTUNITY_EVENT_WATCH"
                    pattern = "PULLBACK_RECLAIM"
                event_id = f"{state['trade_date']}:{current['symbol']}:{event_kind}_{pattern}"
                if event_kind == "OPPORTUNITY" and state.get("opportunity_event_date") == state.get("trade_date"):
                    return None
                if state["last_event"].get(event_kind) == event_id:
                    return None
                state["last_event"][event_kind] = event_id
                if t1_entry_ready:
                    state["buy_event_date"] = state["trade_date"]
                    state["phase"] = "SIGNALLED"
                else:
                    state["opportunity_event_date"] = state["trade_date"]
                return {
                    "event": event_type,
                    "pattern": pattern,
                    "signal_grade": (
                        "UNEXECUTABLE_DISCOVERY" if execution["limit_locked"]
                        else ("T1_NEW_ENTRY" if t1_entry_ready else "TREND_WATCH_OR_EXISTING_POSITION_T")
                    ),
                    "event_id": event_id,
                    "symbol": current["symbol"],
                    "event_ts": now.isoformat(),
                    "price": current["price"],
                    "vwap": vwap or None,
                    "vwap_gap": current["price"] / vwap - 1.0 if vwap > 0 else None,
                    "reference_close": reference_close or None,
                    "intraday_return": current["price"] / reference_close - 1.0 if reference_close > 0 else None,
                    "amount_imbalance": imbalance,
                    "impulse_high": state["impulse_high"],
                    "pullback_low": state["pullback_low"],
                    "recovery_level": recovery_level,
                    "atr14_pct": atr,
                    "impulse_threshold": impulse_threshold,
                    "pullback_min": pullback_min,
                    "pullback_max": pullback_max,
                    "data_quality": "VWAP_AND_OR_TOP5_AMOUNT_AVAILABLE",
                    "auction_gate": auction_gate.get("gate", "NEUTRAL"),
                    "auction_label": auction_gate.get("label", "NO_AUCTION_EVIDENCE"),
                    "daily_route": route,
                    "live_signal_strength": strength,
                    "entry_quality": entry_quality,
                    "composite_signal_strength": composite_strength,
                    "signal_age_minutes": generic_signal_age_minutes,
                    "t1_survivability": t1_survivability,
                    "entry_tier": t1_survivability.get("grade") if t1_entry_ready else "WATCH",
                    "live_sector": live_sector,
                    "multitimeframe": multitimeframe,
                    "capital_behavior": capital_behavior,
                    "executable": execution["executable"],
                    "limit_locked": execution["limit_locked"],
                    "order_submitted": False,
                }
        return None

    def _evaluate_same_day_continuation_risk(
        self,
        state: Dict[str, Any],
        current: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """T+1不可卖时仍报告强趋势买点已经失效，且绝不伪装成卖出成交。"""
        continuation = candidate.get("continuation_context") or {}
        multitimeframe = candidate.get("multitimeframe") or {}
        capital_behavior = candidate.get("capital_behavior") or {}
        status = str(continuation.get("status") or "")
        score = int(_safe_float(continuation.get("score")))
        now = current["event_ts"]
        rows_60 = self._window(state["samples"], now, 60)
        if len(rows_60) < 2 or (rows_60[-1]["event_ts"] - rows_60[0]["event_ts"]).total_seconds() < 45:
            return None
        momentum_60 = self._momentum(rows_60)
        vwap = _safe_float(current.get("vwap"))
        vwap_gap = current["price"] / vwap - 1.0 if vwap > 0 else None
        sector = candidate.get("live_sector") or {}
        sector_weakened = str(sector.get("state") or "") in {"DIVERGING", "DECAY"}
        continuation_failed = status == "FAILED_ACCEPTANCE" or (
            status == "DEGRADED" and score < 60
            and (sector_weakened or (vwap_gap is not None and vwap_gap <= -0.002))
        )
        drawdown = (
            (state["session_high"] - current["price"]) / state["session_high"]
            if state.get("session_high", 0) > 0 else 0.0
        )
        generic_signal_failed = bool(
            status in {"NOT_APPLICABLE", "NO_LIVE_CONTEXT", ""}
            and vwap_gap is not None and vwap_gap <= -0.002
            and (sector_weakened or drawdown >= 0.012)
        )
        multitimeframe_failed = bool(
            multitimeframe.get("periods")
            and (
                multitimeframe.get("alignment") == "BEARISH_2_OF_3"
                or int(_safe_float(multitimeframe.get("bearish_count"))) >= 2
            )
            and vwap_gap is not None and vwap_gap <= -0.002
        )
        capital_failed = bool(
            capital_behavior.get("status") == "READY"
            and capital_behavior.get("confidence") == "HIGH"
            and capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
            and vwap_gap is not None and vwap_gap <= -0.001
        )
        failed = continuation_failed or generic_signal_failed or multitimeframe_failed or capital_failed
        if not failed or momentum_60 >= 0:
            state["same_day_risk_candidate_at"] = None
            return None
        if state.get("same_day_risk_candidate_at") is None:
            state["same_day_risk_candidate_at"] = now
            return None
        risk_persistence = (now - state["same_day_risk_candidate_at"]).total_seconds()
        if risk_persistence < 45:
            return None
        event_id = f"{state['trade_date']}:{current['symbol']}:RISK:SAME_DAY_CONTINUATION_INVALIDATION"
        if state["last_event"].get("RISK") == event_id:
            return None
        state["last_event"]["RISK"] = event_id
        return {
            "event": "RISK_EVENT_WATCH",
            "pattern": "SAME_DAY_CONTINUATION_INVALIDATION",
            "event_id": event_id,
            "symbol": current["symbol"],
            "event_ts": now.isoformat(),
            "price": current["price"],
            "vwap": vwap or None,
            "vwap_gap": vwap_gap,
            "momentum_60s": momentum_60,
            "session_drawdown": drawdown,
            "continuation": continuation,
            "multitimeframe": multitimeframe,
            "capital_behavior": capital_behavior,
            "live_sector": sector,
            "position_entry_date": candidate.get("position_entry_date"),
            "position_entry_price": candidate.get("position_entry_price"),
            "t_plus_one_blocked": True,
            "risk_persistence_seconds": risk_persistence,
            "order_submitted": False,
        }

    def _evaluate_position_recovery(
        self,
        state: Dict[str, Any],
        current: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """减仓后资金和结构重新被接受时，给出一次恢复提示而不自动回补。"""

        if int(state.get("sell_emitted_tier", 0)) != 1 or state.get("position_recovery_emitted"):
            return None
        now = current["event_ts"]
        rows_60 = self._window(state["samples"], now, 60)
        if len(rows_60) < 2 or (rows_60[-1]["event_ts"] - rows_60[0]["event_ts"]).total_seconds() < 45:
            return None
        momentum_60 = self._momentum(rows_60)
        vwap = _safe_float(current.get("vwap"))
        vwap_gap = current["price"] / vwap - 1.0 if vwap > 0 else None
        capital = candidate.get("capital_behavior") or {}
        sector = candidate.get("live_sector") or {}
        market_sector = candidate.get("market_sector") or {}
        multitimeframe = candidate.get("multitimeframe") or {}
        capital_recovered = bool(
            capital.get("phase") in {"AGGRESSIVE_INFLOW", "CONTROLLED_ADVANCE", "SELLING_ABSORBED"}
            and (
                capital.get("flow_persistence_confirmed")
                or capital.get("entry_support")
                or capital.get("hold_support")
            )
        )
        sector_recovered = bool(
            str(sector.get("state") or "") != "DECAY"
            and str(market_sector.get("rotation_state") or "") not in {"ROTATION_OUT", "WEAK"}
            and not market_sector.get("rotation_caution")
        )
        mtf_recovered = bool(
            multitimeframe.get("alignment") != "BEARISH_2_OF_3"
            and int(_safe_float(multitimeframe.get("bearish_count"))) < 2
        )
        recovered = bool(
            vwap_gap is not None and vwap_gap >= 0.002
            and momentum_60 >= 0.001
            and capital_recovered and sector_recovered and mtf_recovered
        )
        if not recovered:
            state["position_recovery_candidate_at"] = None
            return None
        if state.get("position_recovery_candidate_at") is None:
            state["position_recovery_candidate_at"] = now
            return None
        persistence = (now - state["position_recovery_candidate_at"]).total_seconds()
        if persistence < 45:
            return None
        event_id = f"{state['trade_date']}:{current['symbol']}:OPPORTUNITY:POSITION_RECOVERY_AFTER_REDUCE"
        if state["last_event"].get("RECOVERY") == event_id:
            return None
        state["last_event"]["RECOVERY"] = event_id
        state["position_recovery_emitted"] = True
        return {
            "event": "OPPORTUNITY_EVENT_WATCH",
            "pattern": "POSITION_RECOVERY_AFTER_REDUCE",
            "signal_grade": "POSITION_RECOVERY_NOT_AUTOMATIC_REBUY",
            "event_id": event_id,
            "symbol": current["symbol"],
            "event_ts": now.isoformat(),
            "price": current["price"],
            "vwap": vwap,
            "vwap_gap": vwap_gap,
            "momentum_60s": momentum_60,
            "capital_behavior": capital,
            "live_sector": sector,
            "market_sector": market_sector,
            "multitimeframe": multitimeframe,
            "recovery_persistence_seconds": persistence,
            "position_entry_date": candidate.get("position_entry_date"),
            "position_entry_price": candidate.get("position_entry_price"),
            "order_submitted": False,
        }

    def _evaluate_sell(
        self,
        state: Dict[str, Any],
        current: Dict[str, Any],
        candidate: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        entry_date = str(candidate.get("position_entry_date") or "")[:10]
        if state.get("buy_event_date") == state.get("trade_date") or entry_date == state.get("trade_date"):
            return None
        now = current["event_ts"]
        rows_60 = self._window(state["samples"], now, 60)
        if len(rows_60) < 2 or (rows_60[-1]["event_ts"] - rows_60[0]["event_ts"]).total_seconds() < 45:
            return None
        atr = min(max(_safe_float(candidate.get("atr14_pct"), 0.05), 0.015), 0.15)
        drawdown_trigger = min(max(0.08 * atr, 0.004), 0.015)
        drawdown = (state["session_high"] - current["price"]) / state["session_high"] if state["session_high"] > 0 else 0.0
        momentum_60 = self._momentum(rows_60)
        vwap = _safe_float(current.get("vwap"))
        below_vwap = vwap > 0 and current["price"] < vwap
        vwap_gap = current["price"] / vwap - 1.0 if vwap > 0 else None
        imbalance = current.get("amount_imbalance")
        pressure = imbalance is not None and imbalance <= -0.20
        micro_confirmed = below_vwap or pressure
        daily_exit_reversal = candidate.get("action") == "EXIT" and drawdown >= drawdown_trigger and momentum_60 <= -0.0015 and micro_confirmed

        entry_price = _safe_float(candidate.get("position_entry_price"))
        unrealized = current["price"] / entry_price - 1.0 if entry_price > 0 else None
        session_evidence_seconds = (
            (now - state["samples"][0]["event_ts"]).total_seconds()
            if state.get("samples") else 0.0
        )
        entry_route = str(candidate.get("position_entry_route") or candidate.get("daily_route") or "")
        entry_pattern = str(candidate.get("position_entry_pattern") or "")
        if entry_pattern == "SUDDEN_TREND_BREAKOUT" or entry_route in {"SUDDEN_TREND", "NO_SETUP"}:
            route_profile = "SUDDEN_MOMENTUM"
            hard_stop_threshold = min(max(0.48 * atr, 0.025), 0.075)
            profit_activation = min(max(0.48 * atr, 0.025), 0.075)
            profit_pullback = min(max(0.16 * atr, 0.007), 0.022)
        elif entry_route == "TREND_PULLBACK":
            route_profile = "PULLBACK_STRUCTURE"
            hard_stop_threshold = min(max(0.52 * atr, 0.025), 0.080)
            profit_activation = min(max(0.52 * atr, 0.025), 0.080)
            profit_pullback = min(max(0.18 * atr, 0.007), 0.025)
        else:
            route_profile = "TREND_CONTINUATION"
            # 主升趋势给正常换手留出空间，避免一个小周期回踩就清掉大肉。
            hard_stop_threshold = min(max(0.70 * atr, 0.035), 0.10)
            profit_activation = min(max(0.75 * atr, 0.035), 0.11)
            profit_pullback = min(max(0.25 * atr, 0.010), 0.035)
        catastrophic_open_loss = bool(
            unrealized is not None
            and unrealized <= -max(hard_stop_threshold + 0.02, 0.05)
        )
        opening_exit_evidence_ready = bool(session_evidence_seconds >= 180 or catastrophic_open_loss)
        route_tactical_loss_threshold = min(max(0.20 * atr, 0.010), 0.025)
        position_materially_underwater = bool(
            unrealized is not None and unrealized <= -route_tactical_loss_threshold
        )
        position_has_protectable_profit = bool(
            entry_price > 0
            and (state["session_high"] / entry_price - 1.0) >= max(profit_activation * 0.60, 0.020)
        )
        position_risk_location_confirmed = bool(
            candidate.get("action") == "EXIT"
            or position_materially_underwater
            or position_has_protectable_profit
        )
        virtual_loss_protection = (
            bool(candidate.get("monitor_sell")) and unrealized is not None
            and unrealized <= -hard_stop_threshold and momentum_60 < 0 and micro_confirmed
            and opening_exit_evidence_ready
        )
        virtual_hard_stop = bool(
            virtual_loss_protection
            and unrealized is not None
            and unrealized <= -max(hard_stop_threshold * 1.35, hard_stop_threshold + 0.015)
        )
        virtual_profit_protect = (
            bool(candidate.get("monitor_sell")) and unrealized is not None
            and (state["session_high"] / entry_price - 1.0) >= profit_activation
            and drawdown >= profit_pullback and momentum_60 <= -0.0015 and micro_confirmed
        )
        live_sector = candidate.get("live_sector") or {}
        multitimeframe = candidate.get("multitimeframe") or {}
        capital_behavior = candidate.get("capital_behavior") or {}
        market_permission = candidate.get("market_permission") or {}
        market_sector = candidate.get("market_sector") or {}
        mtf_available = bool(multitimeframe.get("periods"))
        five_period = (multitimeframe.get("periods") or {}).get("5") or {}
        thirty_period = (multitimeframe.get("periods") or {}).get("30") or {}
        sixty_period = (multitimeframe.get("periods") or {}).get("60") or {}
        mtf_bearish = bool(
            multitimeframe.get("alignment") == "BEARISH_2_OF_3"
            or int(_safe_float(multitimeframe.get("bearish_count"))) >= 2
        )
        five_bearish = bool(five_period.get("bearish") or five_period.get("state") == "BEARISH")
        sector_state = str(live_sector.get("state") or "UNAVAILABLE")
        sector_role = str(live_sector.get("role") or "UNCLASSIFIED")
        sector_relative_weakness = (
            bool(candidate.get("monitor_sell"))
            and opening_exit_evidence_ready
            and position_risk_location_confirmed
            and sector_state in {"DECAY", "DIVERGING"}
            and sector_role == "LAGGARD"
            and drawdown >= drawdown_trigger
            and momentum_60 <= -0.0015
            and micro_confirmed
            and (not mtf_available or five_bearish)
        )
        # 5/15/30分钟转弱首先是“减仓/保护”证据，不等同于日线趋势结束。旧逻辑只要
        # 两个小周期转弱就发卖出，容易在强趋势正常换手时过早清掉（药明康德型漏肉）。
        # 现在至少再要求资金流出、板块掉队、日线退出或持仓已转亏之一共同确认。
        multitimeframe_trend_reversal = bool(
            candidate.get("monitor_sell") and mtf_bearish
            and opening_exit_evidence_ready
            and position_risk_location_confirmed
            and drawdown >= max(drawdown_trigger, 0.008)
            and momentum_60 <= -0.0015 and micro_confirmed
            and (
                candidate.get("action") == "EXIT"
                or capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
                or (sector_state in {"DECAY", "DIVERGING"} and sector_role == "LAGGARD")
                or (unrealized is not None and unrealized < 0)
            )
        )
        capital_outflow_confirmed = bool(
            candidate.get("monitor_sell")
            and opening_exit_evidence_ready
            and position_risk_location_confirmed
            and capital_behavior.get("status") == "READY"
            and capital_behavior.get("confidence") == "HIGH"
            and capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
            and _safe_float(capital_behavior.get("score")) <= 42
            and drawdown >= max(drawdown_trigger * 0.70, 0.003)
            and momentum_60 < 0
            and micro_confirmed
        )
        # T+1 首个可卖交易日不能继续沿用“给主升趋势足够波动空间”的宽止损。
        # 若新仓在次日经过至少十分钟后仍无法完成延续接受，并同时落到成本与
        # VWAP下方，再叠加板块/多周期/资金中的至少两类弱化证据，先降低风险。
        # 这不是单一亏损阈值止损：正常回踩、板块仍强或迅速收复VWAP都不会触发。
        continuation_context = candidate.get("continuation_context") or {}
        continuation_status = str(continuation_context.get("status") or "")
        continuation_score = _safe_float(continuation_context.get("score"), 50.0)
        next_day_failure_threshold = min(max(0.25 * atr, 0.012), 0.025)
        continuation_failed = bool(
            continuation_status == "FAILED_ACCEPTANCE"
            or (continuation_status == "DEGRADED" and continuation_score < 58)
        )
        sector_failed = bool(
            sector_role == "LAGGARD" and sector_state in {"DECAY", "DIVERGING"}
        )
        market_sector_failed = bool(
            str(market_sector.get("rotation_state") or "") in {"ROTATION_OUT", "WEAK"}
            or market_sector.get("rotation_caution")
        )
        capital_warning = bool(
            capital_behavior.get("status") == "READY"
            and capital_behavior.get("confidence") == "HIGH"
            and capital_behavior.get("phase") in {"OUTFLOW_WARNING", "CONFIRMED_OUTFLOW"}
            and _safe_float(capital_behavior.get("score"), 50.0) <= 48
        )
        next_day_failure_evidence = sum(
            int(value) for value in (
                continuation_failed, sector_failed, market_sector_failed, mtf_bearish, capital_warning,
            )
        )
        t1_failed_continuation = bool(
            candidate.get("monitor_sell")
            and route_profile == "TREND_CONTINUATION"
            and entry_date and entry_date < str(state.get("trade_date") or "")
            and session_evidence_seconds >= 600
            and unrealized is not None and unrealized <= -next_day_failure_threshold
            and vwap_gap is not None and vwap_gap <= -0.003
            and momentum_60 < 0
            and next_day_failure_evidence >= 2
            and (continuation_failed or (sector_failed and market_sector_failed))
        )
        reduce_emitted_at = state.get("position_reduce_emitted_at")
        reduce_age_seconds = (
            (now - reduce_emitted_at).total_seconds()
            if isinstance(reduce_emitted_at, datetime) else 0.0
        )
        next_day_failure_exit_threshold = min(max(next_day_failure_threshold * 1.45, 0.025), 0.045)
        t1_failed_continuation_exit = bool(
            candidate.get("monitor_sell")
            and route_profile == "TREND_CONTINUATION"
            and int(state.get("sell_emitted_tier", 0)) == 1
            and reduce_age_seconds >= 600
            and unrealized is not None and unrealized <= -next_day_failure_exit_threshold
            and vwap_gap is not None and vwap_gap <= -0.006
            and momentum_60 < 0
            and continuation_failed and sector_failed and market_sector_failed
            and (mtf_bearish or capital_warning)
        )
        volume_factor = candidate.get("volume_soft_factor") or {}
        top_volume_defense_confirmed = bool(
            candidate.get("monitor_sell")
            and volume_factor.get("blocks_new_entry")
            and str(volume_factor.get("risk_level")) in {"HIGH", "EXTREME"}
            and momentum_60 <= -0.0010
            and micro_confirmed
            and (drawdown >= max(drawdown_trigger * 0.60, 0.003) or pressure)
        )
        # 市场层只在A股自身已进入RISK_OFF/PANIC，且个股也出现价格/动量及
        # 小周期、板块或资金弱化时，才成为“减弱仓”的共同证据。外围风险
        # 单独存在不进入此条件，也绝不产生集体卖出。
        market_risk_weak_position = bool(
            candidate.get("monitor_sell")
            and market_permission.get("domestic_source") == "FULL_MARKET_INTRADAY"
            and market_permission.get("state") in {"RISK_OFF", "PANIC_CLEARING"}
            and drawdown >= max(drawdown_trigger, 0.005)
            and momentum_60 <= -0.0015
            and micro_confirmed
            and (
                five_bearish
                or sector_state in {"DECAY", "DIVERGING"}
                or capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
            )
        )
        confirmed_30_bearish_divergence = bool(
            thirty_period.get("macd_divergence") == "BEARISH"
            and thirty_period.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
            and int(_safe_float(thirty_period.get("divergence_quality"))) >= 55
        )
        confirmed_60_bearish_divergence = bool(
            sixty_period.get("macd_divergence") == "BEARISH"
            and sixty_period.get("divergence_lifecycle") == "CONFIRMED_ACTIVE"
            and int(_safe_float(sixty_period.get("divergence_quality"))) >= 55
        )
        divergence_risk_reduce = bool(
            candidate.get("monitor_sell")
            and (confirmed_30_bearish_divergence or confirmed_60_bearish_divergence)
            and drawdown >= max(drawdown_trigger * 0.70, 0.004)
            and momentum_60 < 0 and micro_confirmed
            and (
                capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
                or (sector_state in {"DECAY", "DIVERGING"} and sector_role in {"LAGGARD", "FOLLOWER"})
                or candidate.get("action") == "EXIT"
            )
        )
        risk_levels = (candidate.get("price_battle_plan") or {}).get("risk_levels") or {}
        structure_failure_below = _safe_float(risk_levels.get("structure_failure_below"))
        structure_failure_exit = bool(
            candidate.get("monitor_sell")
            and structure_failure_below > 0
            and current["price"] <= structure_failure_below * 0.998
            and momentum_60 < 0 and micro_confirmed
            and (
                capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
                or mtf_bearish
                or sector_state in {"DECAY", "DIVERGING"}
            )
        )
        dual_divergence_exit = bool(
            candidate.get("monitor_sell")
            and confirmed_30_bearish_divergence and confirmed_60_bearish_divergence
            and drawdown >= max(drawdown_trigger, 0.006)
            and momentum_60 <= -0.0015 and below_vwap
            and (
                capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
                or sector_state in {"DECAY", "DIVERGING"}
                or candidate.get("action") == "EXIT"
            )
        )
        sixty_trend_broken = bool(
            sixty_period.get("state") == "BEARISH"
            and _safe_float(sixty_period.get("close"), current["price"])
            < _safe_float(sixty_period.get("ma20"), current["price"] + 1)
        )
        confirmed_trend_exit = bool(
            candidate.get("monitor_sell")
            and candidate.get("action") == "EXIT"
            and sixty_trend_broken
            and drawdown >= max(drawdown_trigger, 0.006)
            and momentum_60 < 0 and micro_confirmed
            and (
                capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
                or sector_state in {"DECAY", "DIVERGING"}
                or confirmed_30_bearish_divergence
            )
        )
        panic_full_exit = bool(
            market_risk_weak_position
            and market_permission.get("state") == "PANIC_CLEARING"
            and capital_behavior.get("phase") == "CONFIRMED_OUTFLOW"
            and (mtf_bearish or confirmed_30_bearish_divergence or confirmed_60_bearish_divergence)
        )
        if (
            daily_exit_reversal or virtual_loss_protection or virtual_hard_stop or virtual_profit_protect
            or sector_relative_weakness or multitimeframe_trend_reversal or capital_outflow_confirmed
            or top_volume_defense_confirmed or market_risk_weak_position or divergence_risk_reduce
            or structure_failure_exit or dual_divergence_exit or confirmed_trend_exit or panic_full_exit
            or t1_failed_continuation or t1_failed_continuation_exit
        ):
            pattern = (
                "VIRTUAL_STOP_LOSS" if virtual_hard_stop
                else (
                    "T1_FAILED_CONTINUATION_EXIT" if t1_failed_continuation_exit
                    else (
                    "STRUCTURE_FAILURE_EXIT" if structure_failure_exit
                    else (
                        "DUAL_30_60_DIVERGENCE_EXIT" if dual_divergence_exit
                        else (
                            "CONFIRMED_TREND_EXIT" if confirmed_trend_exit
                            else (
                                "PANIC_WEAK_POSITION_EXIT" if panic_full_exit
                                else (
                                    "T1_FAILED_CONTINUATION_PROTECTION" if t1_failed_continuation
                                    else (
                                    "VIRTUAL_LOSS_PROTECTION" if virtual_loss_protection
                                    else (
                                        "PROFIT_PROTECTION_REVERSAL" if virtual_profit_protect
                                        else (
                                            "SECTOR_RELATIVE_WEAKNESS" if sector_relative_weakness
                                            else (
                                                "MULTITIMEFRAME_TREND_REVERSAL" if multitimeframe_trend_reversal
                                                else (
                                                    "CAPITAL_OUTFLOW_CONFIRMED" if capital_outflow_confirmed
                                                    else (
                                                        "TOP_VOLUME_CONTRACTION_DEFENSE" if top_volume_defense_confirmed
                                                        else (
                                                            "MACD_30_60_DIVERGENCE_RISK"
                                                            if divergence_risk_reduce
                                                            else ("MARKET_RISK_WEAK_POSITION" if market_risk_weak_position else "DAILY_EXIT_FAILED_HIGH")
                                                        )
                                                    )
                                                )
                                            )
                                        )
                                    )
                                    )
                                )
                            )
                        )
                    )
                )
                )
            )
            full_exit_patterns = {
                "VIRTUAL_STOP_LOSS", "STRUCTURE_FAILURE_EXIT", "DUAL_30_60_DIVERGENCE_EXIT",
                "CONFIRMED_TREND_EXIT", "PANIC_WEAK_POSITION_EXIT", "T1_FAILED_CONTINUATION_EXIT",
            }
            exit_tier = "EXIT" if pattern in full_exit_patterns else "REDUCE"
            tier_rank = 2 if exit_tier == "EXIT" else 1
            # 同一交易日同一标的最多发一次减仓，之后只有证据升级为全退出才再发。
            # 防止多个相近规则交替命中造成飞书刷屏和行动冲突。
            if tier_rank <= int(state.get("sell_emitted_tier", 0)):
                return None
            persistence_required = 0 if pattern == "VIRTUAL_STOP_LOSS" else (20 if exit_tier == "EXIT" else 30)
            if state.get("sell_candidate_key") != pattern:
                state["sell_candidate_key"] = pattern
                state["sell_candidate_at"] = now
                if persistence_required > 0:
                    return None
            confirmation_persistence = (
                (now - state["sell_candidate_at"]).total_seconds()
                if state.get("sell_candidate_at") is not None else 0.0
            )
            if confirmation_persistence < persistence_required:
                return None
            event_id = f"{state['trade_date']}:{current['symbol']}:SELL:{pattern}"
            if state["last_event"].get("SELL") == event_id:
                return None
            state["last_event"]["SELL"] = event_id
            state["sell_emitted_tier"] = tier_rank
            if tier_rank == 1 and state.get("position_reduce_emitted_at") is None:
                state["position_reduce_emitted_at"] = now
            return {
                "event": "SELL_EVENT_WATCH",
                "pattern": pattern,
                "event_id": event_id,
                "symbol": current["symbol"],
                "event_ts": now.isoformat(),
                "price": current["price"],
                "session_high": state["session_high"],
                "drawdown": drawdown,
                "momentum_60s": momentum_60,
                "vwap": vwap or None,
                "vwap_gap": vwap_gap,
                "amount_imbalance": imbalance,
                "daily_slow_j": candidate.get("slow_j"),
                "position_entry_date": entry_date or None,
                "position_entry_price": entry_price or None,
                "unrealized_return": unrealized,
                "hard_stop_threshold": hard_stop_threshold,
                "route_tactical_loss_threshold": route_tactical_loss_threshold,
                "position_materially_underwater": position_materially_underwater,
                "position_has_protectable_profit": position_has_protectable_profit,
                "profit_activation": profit_activation,
                "profit_pullback": profit_pullback,
                "entry_route": entry_route,
                "entry_pattern": entry_pattern,
                "exit_route_profile": route_profile,
                "exit_tier": exit_tier,
                "confirmation_persistence_seconds": confirmation_persistence,
                "session_evidence_seconds": session_evidence_seconds,
                "opening_exit_evidence_ready": opening_exit_evidence_ready,
                "catastrophic_open_loss": catastrophic_open_loss,
                "next_day_failure_threshold": next_day_failure_threshold,
                "next_day_failure_exit_threshold": next_day_failure_exit_threshold,
                "reduce_age_seconds": reduce_age_seconds,
                "next_day_failure_evidence_count": next_day_failure_evidence,
                "next_day_failure_context": {
                    "continuation_failed": continuation_failed,
                    "sector_failed": sector_failed,
                    "market_sector_failed": market_sector_failed,
                    "multitimeframe_bearish": mtf_bearish,
                    "capital_warning": capital_warning,
                    "minimum_session_evidence_seconds": 600,
                },
                "structure_failure_below": structure_failure_below or None,
                "live_sector": live_sector,
                "multitimeframe": multitimeframe,
                "capital_behavior": capital_behavior,
                "divergence_risk_context": {
                    "thirty_confirmed_bearish": confirmed_30_bearish_divergence,
                    "sixty_confirmed_bearish": confirmed_60_bearish_divergence,
                    "role": "REDUCE_ONLY_WITH_PRICE_PLUS_CAPITAL_OR_SECTOR_CONFIRMATION",
                },
                "exit_conviction": (
                    "HIGH" if exit_tier == "EXIT"
                    else "MEDIUM" if pattern in {"MULTITIMEFRAME_TREND_REVERSAL", "CAPITAL_OUTFLOW_CONFIRMED", "MACD_30_60_DIVERGENCE_RISK"}
                    else "PROTECTIVE"
                ),
                "order_submitted": False,
            }
        state["sell_candidate_at"] = None
        state["sell_candidate_key"] = None
        return None

    def snapshot(self) -> Dict[str, Any]:
        rows = []
        for symbol, state in self.states.items():
            samples = state.get("samples") or []
            if not samples:
                continue
            current = samples[-1]
            rows.append({
                "symbol": symbol,
                "trade_date": state.get("trade_date"),
                "phase": state.get("phase"),
                "entry_state": (
                    "SIGNALLED" if state.get("buy_event_date") == state.get("trade_date")
                    else state.get("opportunity_pattern") or state.get("phase")
                ),
                "price": current.get("price"),
                "event_ts": current.get("event_ts").isoformat() if current.get("event_ts") else None,
                "vwap": current.get("vwap") or None,
                "amount_imbalance": current.get("amount_imbalance"),
                "session_high": state.get("session_high"),
                "session_low": state.get("session_low"),
                "sample_count": len(samples),
                "last_event": state.get("last_event"),
                "opportunity_stage": state.get("opportunity_stage", 0),
                "trend_armed": bool(state.get("trend_armed")),
            })
        return {"rows": rows, "by_symbol": {row["symbol"]: row for row in rows}}
