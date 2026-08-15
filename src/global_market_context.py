# coding: utf-8
"""外围市场风险温度。

外围市场只修正盘前风险预算；它不能单独触发A股买入、减仓或卖出。接口失败、
时间戳陈旧或覆盖不足时返回UNKNOWN，绝不把数据故障误判成风险事件。
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional


GLOBAL_MARKET_CONTEXT_VERSION = "global_market_context_v1_low_weight"
GLOBAL_HOSTS = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get?",
    "https://push2.eastmoney.com/api/qt/ulist.np/get?",
)
GLOBAL_INDICES = {
    "DJIA": {"name_cn": "道琼斯", "region": "US", "weight": 0.12},
    "NDX": {"name_cn": "纳斯达克", "region": "US", "weight": 0.23},
    "SPX": {"name_cn": "标普500", "region": "US", "weight": 0.20},
    "N225": {"name_cn": "日经225", "region": "ASIA", "weight": 0.18},
    "KS11": {"name_cn": "韩国KOSPI", "region": "ASIA", "weight": 0.17},
    "HSI": {"name_cn": "恒生指数", "region": "HK", "weight": 0.10},
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


class GlobalMarketMonitor:
    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        self.latest: Dict[str, Any] = {
            "version": GLOBAL_MARKET_CONTEXT_VERSION,
            "status": "UNINITIALIZED",
            "state": "UNKNOWN",
            "score_adjustment": 0,
            "rows": [],
        }

    def _fetch(self) -> Dict[str, Any]:
        query = urllib.parse.urlencode({
            "fltt": 2,
            "fields": "f12,f13,f14,f2,f3,f4,f15,f16,f17,f18,f124",
            "secids": ",".join(f"100.{code}" for code in GLOBAL_INDICES),
        })
        last_error: Optional[Exception] = None
        for host in GLOBAL_HOSTS:
            try:
                request = urllib.request.Request(
                    host + query,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8", errors="ignore") or "{}")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"{type(last_error).__name__}: {str(last_error)[:180]}")

    @staticmethod
    def classify(rows: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        usable = [row for row in rows if row.get("freshness") != "STALE" and row.get("pct") is not None]
        if len(usable) < 3:
            return {
                "state": "UNKNOWN", "state_cn": "外围数据不足", "risk_score": 50,
                "score_adjustment": 0, "coverage": len(usable),
                "reason": "可用外围指数不足3个，不参与盘前判断",
            }
        total_weight = sum(_safe_float(row.get("weight")) for row in usable)
        weighted_return = (
            sum(_safe_float(row.get("pct")) * _safe_float(row.get("weight")) for row in usable)
            / max(total_weight, 1e-9)
        )
        negative_1pct = sum(_safe_float(row.get("pct")) <= -0.01 for row in usable)
        positive_1pct = sum(_safe_float(row.get("pct")) >= 0.01 for row in usable)
        us = [row for row in usable if row.get("region") == "US"]
        asia = [row for row in usable if row.get("region") == "ASIA"]
        us_mean = sum(_safe_float(row.get("pct")) for row in us) / len(us) if us else 0.0
        asia_mean = sum(_safe_float(row.get("pct")) for row in asia) / len(asia) if asia else 0.0

        if weighted_return <= -0.018 or negative_1pct >= 4 or (us_mean <= -0.015 and asia_mean <= -0.012):
            state, state_cn, adjustment = "RISK_OFF", "外围明显承压", -5
            reason = "多个外围指数同步下跌，只降低A股盘前风险预算；仍须等待A股自身确认"
        elif weighted_return <= -0.006 or negative_1pct >= 2:
            state, state_cn, adjustment = "CAUTION", "外围偏谨慎", -2
            reason = "外围风险偏好偏弱，提高A股新仓确认门槛但不机械砍仓"
        elif weighted_return >= 0.009 and positive_1pct >= 2:
            state, state_cn, adjustment = "RISK_ON", "外围偏积极", 2
            reason = "外围风险偏好较好，仅提供小幅正向先验，不替代A股盘中资金确认"
        else:
            state, state_cn, adjustment = "NEUTRAL", "外围中性", 0
            reason = "外围没有形成一致方向，以A股自身板块和资金行为为主"
        risk_score = int(round(max(0.0, min(100.0, 50.0 - weighted_return * 1250.0))))
        return {
            "state": state,
            "state_cn": state_cn,
            "risk_score": risk_score,
            "score_adjustment": int(max(-5, min(2, adjustment))),
            "coverage": len(usable),
            "weighted_return": round(weighted_return, 6),
            "us_mean": round(us_mean, 6),
            "asia_mean": round(asia_mean, 6),
            "negative_1pct_count": negative_1pct,
            "positive_1pct_count": positive_1pct,
            "reason": reason,
            "decision_role": "LOW_WEIGHT_RISK_BUDGET_NOT_TRADE_TRIGGER",
        }

    def refresh(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now()
        try:
            payload = self._fetch()
            rows = []
            for item in ((payload.get("data") or {}).get("diff") or []):
                code = str(item.get("f12") or "")
                meta = GLOBAL_INDICES.get(code)
                if not meta:
                    continue
                epoch = int(_safe_float(item.get("f124"), 0))
                source_ts = datetime.fromtimestamp(epoch) if epoch > 0 else None
                # 美股盘前读取的是最近一个完整交易日，允许3个自然日；亚太当日盘中只允许1日。
                max_age_days = 3 if meta["region"] == "US" else 1
                age_days = (now.date() - source_ts.date()).days if source_ts else 999
                # 历史回放若意外读到真实世界的未来快照，必须判为STALE，避免外围数据穿越。
                freshness = "FRESH" if source_ts and 0 <= age_days <= max_age_days else "STALE"
                rows.append({
                    "code": code,
                    "name": str(item.get("f14") or meta["name_cn"]),
                    "name_cn": meta["name_cn"],
                    "region": meta["region"],
                    "weight": meta["weight"],
                    "price": _safe_float(item.get("f2")),
                    "pct": _safe_float(item.get("f3")) / 100.0,
                    "source_timestamp": source_ts.isoformat() if source_ts else None,
                    "freshness": freshness,
                })
            classification = self.classify(rows, now=now)
            self.latest = {
                "version": GLOBAL_MARKET_CONTEXT_VERSION,
                "status": "READY" if classification["state"] != "UNKNOWN" else "DEGRADED",
                "asof": now.isoformat(),
                "source": "eastmoney_global_index_snapshot",
                "rows": rows,
                **classification,
            }
        except Exception as exc:
            self.latest = {
                "version": GLOBAL_MARKET_CONTEXT_VERSION,
                "status": "UNAVAILABLE",
                "asof": now.isoformat(),
                "state": "UNKNOWN",
                "state_cn": "外围数据不可用",
                "risk_score": 50,
                "score_adjustment": 0,
                "coverage": 0,
                "rows": [],
                "reason": "外围接口失败，不参与任何买卖判断",
                "error_type": type(exc).__name__,
                "error": str(exc)[:180],
                "decision_role": "NO_EFFECT_ON_FAILURE",
            }
        return self.latest

    @staticmethod
    def compact_line(snapshot: Dict[str, Any]) -> str:
        rows = [row for row in snapshot.get("rows", []) if row.get("freshness") != "STALE"]
        market_text = "｜".join(f"{row.get('name_cn')} {_safe_float(row.get('pct')):+.2%}" for row in rows)
        if not market_text:
            market_text = "无可靠新鲜行情"
        return (
            f"{snapshot.get('state_cn', '外围未知')}｜{market_text}｜"
            f"作用：{snapshot.get('reason', '不参与判断')}"
        )
