#!/usr/bin/env python3
"""Fetch overnight/global tech shock facts for the A-share runtime gate.

This source is deliberately best-effort.  Non-critical provider failures are
kept as reviewable fallback; missing critical gate fields are surfaced to the
runtime, which blocks tech entries until the source is repaired.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - kept for older local Python builds.
    ZoneInfo = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / "reports" / "external_checks"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
YAHOO_SYMBOLS: tuple[dict[str, str], ...] = (
    {"key": "nasdaq", "name": "纳斯达克综合指数", "symbol": "^IXIC", "pct_field": "nasdaq_pct"},
    {"key": "sp500", "name": "标普500", "symbol": "^GSPC", "pct_field": "sp500_pct"},
    {"key": "dow", "name": "道琼斯工业指数", "symbol": "^DJI", "pct_field": "dow_pct"},
    {"key": "sox", "name": "费城半导体指数", "symbol": "^SOX", "pct_field": "sox_pct"},
    {"key": "nvidia", "name": "英伟达", "symbol": "NVDA", "pct_field": "nvidia_pct"},
    {"key": "kospi", "name": "韩国KOSPI", "symbol": "^KS11", "pct_field": "kospi_pct"},
    {"key": "kosdaq", "name": "韩国KOSDAQ", "symbol": "^KQ11", "pct_field": "kosdaq_pct"},
    {"key": "taiwan", "name": "台湾加权指数", "symbol": "^TWII", "pct_field": "taiwan_pct"},
)
THS_LINE_URL = "https://d.10jqka.com.cn/v6/line/88_{code}/01/{kind}.js"
THS_SYMBOLS: tuple[dict[str, str], ...] = (
    {"key": "nasdaq", "name": "纳斯达克综合指数", "code": "IXIC", "pct_field": "nasdaq_pct"},
    {"key": "sp500", "name": "标普500", "code": "SPX", "pct_field": "sp500_pct"},
    {"key": "dow", "name": "道琼斯工业指数", "code": "DJI", "pct_field": "dow_pct"},
    {"key": "kospi", "name": "韩国综合指数", "code": "KS11", "pct_field": "kospi_pct"},
    {"key": "taiwan", "name": "台湾加权指数", "code": "TWII", "pct_field": "taiwan_pct"},
    {"key": "nikkei", "name": "东京日经225指数", "code": "N225", "pct_field": "nikkei_pct"},
)

# These fields are the minimum evidence needed to decide whether an external
# tech shock is present.  Taiwan is intentionally excluded: before its local
# open it is an overnight reference, not a current-session gate input.
EXTERNAL_GATE_REQUIRED_FIELDS: tuple[str, ...] = (
    "nasdaq_pct",
    "sox_pct",
    "nvidia_pct",
    "korea_broad_pct",
)


def compact_date(value: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text[:8]


def shanghai_now() -> dt.datetime:
    if ZoneInfo is None:
        return dt.datetime.now()
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def to_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def assess_external_source_quality(
    metrics: dict[str, Any],
    errors: dict[str, Any],
    ths_errors: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify source health separately from the market-shock decision.

    A provider can fail while another provider supplies every field used by the
    gate.  That is reviewable fallback, not missing evidence.  Conversely, a
    LOW shock score with missing critical fields must remain visibly degraded;
    the classifier's default-pass behavior must never hide that distinction.
    """

    missing = [
        field
        for field in EXTERNAL_GATE_REQUIRED_FIELDS
        if to_float(metrics.get(field)) is None
    ]
    if missing:
        return "degraded_missing_gate_fields", missing
    if errors or ths_errors:
        return "reviewable_live_fallback", []
    return "verified_live", []


def last_two(values: list[Any]) -> tuple[float | None, float | None]:
    clean = [to_float(value) for value in values]
    clean = [value for value in clean if value is not None]
    if not clean:
        return None, None
    if len(clean) == 1:
        return clean[-1], None
    return clean[-1], clean[-2]


def _same_price(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= max(0.01, abs(left) * 0.00001)


def parse_yahoo_chart_payload(payload: dict[str, Any], *, key: str, name: str, symbol: str) -> dict[str, Any]:
    chart = payload.get("chart") if isinstance(payload, dict) else {}
    error = chart.get("error") if isinstance(chart, dict) else None
    if error:
        raise ValueError(str(error))
    result = ((chart.get("result") or []) if isinstance(chart, dict) else [])
    if not result:
        raise ValueError("empty_yahoo_chart_result")
    item = result[0] or {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") if isinstance(quote, dict) else []
    latest_close, previous_close_from_bars = last_two(list(closes or []))
    meta_price = to_float(meta.get("regularMarketPrice"))
    price = meta_price if meta_price is not None else latest_close
    chart_previous_close = to_float(meta.get("chartPreviousClose"))
    prev_close_source = ""
    if meta_price is not None and latest_close is not None and not _same_price(meta_price, latest_close):
        # Yahoo chartPreviousClose is the baseline before the requested range,
        # not necessarily the previous trading day's close.  When daily bars
        # lag the live regularMarketPrice, the latest bar is the safest daily
        # reference.
        prev_close = latest_close
        prev_close_source = "latest_bar_close"
    elif previous_close_from_bars is not None:
        prev_close = previous_close_from_bars
        prev_close_source = "bar_previous_close"
    else:
        prev_close = chart_previous_close
        prev_close_source = "chart_previous_close_fallback"
    pct = ((price - prev_close) / prev_close * 100.0) if price is not None and prev_close else None
    market_ts = meta.get("regularMarketTime")
    market_time = ""
    if market_ts:
        try:
            market_time = dt.datetime.fromtimestamp(int(market_ts), tz=dt.timezone.utc).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError, TypeError):
            market_time = ""
    return {
        "key": key,
        "name": name,
        "symbol": symbol,
        "source": "yahoo_chart",
        "price": round(price, 4) if price is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "prev_close_source": prev_close_source,
        "chart_previous_close": round(chart_previous_close, 4) if chart_previous_close is not None else None,
        "pct": round(pct, 4) if pct is not None else None,
        "currency": meta.get("currency") or "",
        "exchange": meta.get("exchangeName") or meta.get("fullExchangeName") or "",
        "market_time_utc": market_time,
    }


def fetch_yahoo_chart(symbol: str, *, timeout: int) -> dict[str, Any]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = YAHOO_CHART_URL.format(symbol=encoded)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_jsonp_payload(text: str) -> dict[str, Any]:
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("invalid_jsonp_payload")
    return json.loads(text[start + 1 : end])


def fetch_ths_line(code: str, kind: str, *, timeout: int) -> dict[str, Any]:
    url = THS_LINE_URL.format(code=urllib.parse.quote(code, safe=""), kind=kind)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/javascript,text/javascript,*/*",
            "Referer": "https://q.10jqka.com.cn/global/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return parse_jsonp_payload(response.read().decode("utf-8"))


def previous_ths_close(last_payload: dict[str, Any], today_date: str) -> float | None:
    rows = str(last_payload.get("data") or "").split(";")
    closes: list[tuple[str, float]] = []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 5:
            continue
        row_date = compact_date(parts[0])
        close = to_float(parts[4])
        if row_date and close is not None:
            closes.append((row_date, close))
    if not closes:
        return None
    prior = [close for row_date, close in closes if row_date < today_date]
    return prior[-1] if prior else closes[-1][1]


def parse_ths_line_payloads(
    today_payload: dict[str, Any],
    last_payload: dict[str, Any],
    *,
    key: str,
    name: str,
    code: str,
) -> dict[str, Any]:
    row_key = f"88_{code}"
    row = today_payload.get(row_key) if isinstance(today_payload.get(row_key), dict) else {}
    if not row:
        raise ValueError(f"empty_ths_today_row:{code}")
    trade_date = compact_date(row.get("1"))
    price = to_float(row.get("11"))
    open_price = to_float(row.get("7"))
    high = to_float(row.get("8"))
    low = to_float(row.get("9"))
    prev_close = previous_ths_close(last_payload, trade_date)
    pct = ((price - prev_close) / prev_close * 100.0) if price is not None and prev_close else None
    low_pct = ((low - prev_close) / prev_close * 100.0) if low is not None and prev_close else None
    return {
        "key": key,
        "name": row.get("name") or name,
        "code": code,
        "symbol": f"88_{code}",
        "source": "10jqka_v6_line_today",
        "trade_date": trade_date,
        "price": round(price, 4) if price is not None else None,
        "open": round(open_price, 4) if open_price is not None else None,
        "high": round(high, 4) if high is not None else None,
        "low": round(low, 4) if low is not None else None,
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "pct": round(pct, 4) if pct is not None else None,
        "low_pct": round(low_pct, 4) if low_pct is not None else None,
        "dt": row.get("dt") or "",
    }


def collect_yahoo_quotes(*, timeout: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    quotes: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for spec in YAHOO_SYMBOLS:
        try:
            payload = fetch_yahoo_chart(spec["symbol"], timeout=timeout)
            row = parse_yahoo_chart_payload(
                payload,
                key=spec["key"],
                name=spec["name"],
                symbol=spec["symbol"],
            )
            row["pct_field"] = spec["pct_field"]
            quotes.append(row)
        except Exception as exc:
            errors[spec["key"]] = f"{type(exc).__name__}: {exc}"
    return quotes, errors


def collect_ths_quotes(*, timeout: int) -> tuple[list[dict[str, Any]], dict[str, str]]:
    quotes: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for spec in THS_SYMBOLS:
        code = spec["code"]
        try:
            today_payload = fetch_ths_line(code, "today", timeout=timeout)
            last_payload = fetch_ths_line(code, "last", timeout=timeout)
            row = parse_ths_line_payloads(
                today_payload,
                last_payload,
                key=spec["key"],
                name=spec["name"],
                code=code,
            )
            row["pct_field"] = spec["pct_field"]
            quotes.append(row)
        except Exception as exc:
            errors[spec["key"]] = f"{type(exc).__name__}: {exc}"
    return quotes, errors


def pct_by_key(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for quote in quotes:
        pct = to_float(quote.get("pct"))
        field = str(quote.get("pct_field") or "")
        if pct is None or not field:
            continue
        values[field] = round(pct, 4)
    kospi_pct = to_float(values.get("kospi_pct"))
    kosdaq_pct = to_float(values.get("kosdaq_pct"))
    if kospi_pct is not None:
        values["korea_broad_pct"] = round(kospi_pct, 4)
    if kosdaq_pct is not None:
        values["korea_tech_pct"] = round(kosdaq_pct, 4)
        values["korea_pct"] = round(kosdaq_pct, 4)
        values["korea_pct_source"] = "kosdaq"
    elif kospi_pct is not None:
        values["korea_pct"] = round(kospi_pct, 4)
        values["korea_pct_source"] = "kospi_broad_fallback"
    return values


def ths_low_pct_by_key(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for quote in quotes:
        low_pct = to_float(quote.get("low_pct"))
        key = str(quote.get("key") or "")
        if low_pct is None or not key:
            continue
        values[f"{key}_intraday_low_pct"] = round(low_pct, 4)
    kospi_low = to_float(values.get("kospi_intraday_low_pct"))
    if kospi_low is not None:
        values["korea_broad_intraday_low_pct"] = round(kospi_low, 4)
        values["korea_market_stress_pct"] = round(kospi_low, 4)
    return values


def ths_cross_source_warnings(
    yahoo_quotes: list[dict[str, Any]],
    ths_quotes: list[dict[str, Any]],
) -> list[str]:
    yahoo = {str(row.get("key")): to_float(row.get("pct")) for row in yahoo_quotes}
    ths = {str(row.get("key")): to_float(row.get("pct")) for row in ths_quotes}
    warnings: list[str] = []
    for key, label in {"kospi": "韩国KOSPI", "nasdaq": "纳斯达克", "sp500": "标普500", "dow": "道琼斯", "taiwan": "台湾加权"}.items():
        left = yahoo.get(key)
        right = ths.get(key)
        if left is None or right is None:
            continue
        if abs(left - right) >= 1.0:
            warnings.append(f"{label} Yahoo/同花顺涨跌幅差异{left:.2f}% vs {right:.2f}%，已优先采用同花顺")
    return warnings


def parse_market_time_utc(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def previous_session_reference_warnings(
    quotes: list[dict[str, Any]],
    trusted_quotes: list[dict[str, Any]] | None = None,
) -> list[str]:
    warnings: list[str] = []
    now = shanghai_now()
    if now.tzinfo is None or ZoneInfo is None:
        return warnings
    sh_tz = ZoneInfo("Asia/Shanghai")
    today = now.astimezone(sh_tz).date()
    today_compact = today.strftime("%Y%m%d")
    trusted_current_keys = {
        str(quote.get("key") or "")
        for quote in (trusted_quotes or [])
        if compact_date(quote.get("trade_date")) >= today_compact
    }
    for quote in quotes:
        key = str(quote.get("key") or "")
        if key not in {"taiwan"}:
            continue
        if key in trusted_current_keys:
            continue
        market_time = parse_market_time_utc(quote.get("market_time_utc"))
        if market_time is None:
            continue
        local_date = market_time.astimezone(sh_tz).date()
        if local_date < today:
            name = str(quote.get("name") or key)
            warnings.append(f"{name}为{local_date.isoformat()}上一交易日参考；开盘前只作隔夜风险，不作实时盘中确认")
    return warnings


def quote_pct_map(quotes: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in quotes:
        key = str(row.get("key") or "")
        pct = to_float(row.get("pct"))
        if key and pct is not None:
            values[key] = pct
    return values


def downgrade_unconfirmed_korea_tech(
    metrics: dict[str, Any],
    yahoo_quotes: list[dict[str, Any]],
    ths_quotes: list[dict[str, Any]],
) -> list[str]:
    """Keep Korean tech fallback out of hard gates when the live source disagrees.

    Tonghuashun does not currently expose a direct KOSDAQ line source in the
    same endpoint set we use for KOSPI.  When Yahoo's KOSPI is materially out
    of sync with Tonghuashun KOSPI, Yahoo KOSDAQ is useful context but not a
    reliable current-market fact for trading gates.
    """

    warnings: list[str] = []
    yahoo = quote_pct_map(yahoo_quotes)
    ths = quote_pct_map(ths_quotes)
    yahoo_kosdaq = yahoo.get("kosdaq")
    yahoo_kospi = yahoo.get("kospi")
    ths_kospi = ths.get("kospi")
    if yahoo_kosdaq is None:
        return warnings

    delta = abs(yahoo_kospi - ths_kospi) if yahoo_kospi is not None and ths_kospi is not None else None
    source_disagrees = delta is not None and delta >= 1.0
    recovered_in_ths = ths_kospi is not None and ths_kospi > -1.0
    yahoo_broad_stress = yahoo_kospi is not None and yahoo_kospi <= -3.0
    yahoo_tech_stress = yahoo_kosdaq <= -1.5
    if not yahoo_tech_stress or not (source_disagrees or (recovered_in_ths and yahoo_broad_stress)):
        metrics["korea_tech_source_quality"] = "yahoo_fallback"
        return warnings

    raw_kosdaq = metrics.pop("kosdaq_pct", round(yahoo_kosdaq, 4))
    raw_korea_tech = metrics.pop("korea_tech_pct", raw_kosdaq)
    metrics["kosdaq_pct_yahoo_unconfirmed"] = round(to_float(raw_kosdaq) or yahoo_kosdaq, 4)
    metrics["korea_tech_pct_yahoo_unconfirmed"] = round(to_float(raw_korea_tech) or yahoo_kosdaq, 4)
    if metrics.get("korea_pct_source") == "kosdaq":
        raw_korea = metrics.pop("korea_pct", raw_korea_tech)
        metrics["korea_pct_yahoo_unconfirmed"] = round(to_float(raw_korea) or yahoo_kosdaq, 4)
        broad_pct = to_float(metrics.get("korea_broad_pct") or metrics.get("kospi_pct"))
        if broad_pct is not None:
            metrics["korea_pct"] = round(broad_pct, 4)
            metrics["korea_pct_source"] = "kospi_broad_fallback"
        else:
            metrics["korea_pct_source"] = "kosdaq_yahoo_unconfirmed"
    metrics["korea_tech_source_quality"] = "unconfirmed_yahoo_disagrees_with_ths_kospi"
    detail = ""
    if yahoo_kospi is not None and ths_kospi is not None:
        detail = f"（Yahoo KOSPI {yahoo_kospi:.2f}% vs 同花顺KOSPI {ths_kospi:.2f}%）"
    warnings.append(
        f"韩国KOSDAQ仅Yahoo源且韩国宽基同花顺/ Yahoo 不同步{detail}，已降级为参考，不作为韩国科技当前跌幅硬门控"
    )
    return warnings


def derive_risk_flags(metrics: dict[str, Any], *, korea_circuit_breaker: bool) -> dict[str, Any]:
    korea_tech_pct = to_float(metrics.get("korea_tech_pct") or metrics.get("kosdaq_pct"))
    korea_broad_pct = to_float(metrics.get("korea_broad_pct") or metrics.get("kospi_pct"))
    korea_market_stress_pct = to_float(metrics.get("korea_market_stress_pct") or metrics.get("korea_broad_intraday_low_pct"))
    severe_markers = [
        metrics.get("nasdaq_pct") is not None and metrics["nasdaq_pct"] <= -2.0,
        metrics.get("sox_pct") is not None and metrics["sox_pct"] <= -3.0,
        metrics.get("nvidia_pct") is not None and metrics["nvidia_pct"] <= -4.0,
        korea_tech_pct is not None and korea_tech_pct <= -3.0,
        korea_market_stress_pct is not None and korea_market_stress_pct <= -4.0,
        metrics.get("taiwan_pct") is not None and metrics["taiwan_pct"] <= -3.0,
    ]
    # A large move is not proof of an exchange circuit breaker.  This flag is
    # true only when the operator/source supplies explicit breaker evidence.
    asia_breaker = bool(korea_circuit_breaker)
    return {
        "korea_circuit_breaker": asia_breaker,
        "asia_tech_circuit_breaker": asia_breaker,
        "quant_panic": sum(1 for marker in severe_markers if marker) >= 2 or asia_breaker,
        "deep_water_limit_down_risk": sum(1 for marker in severe_markers if marker) >= 3 or asia_breaker,
    }


def classify_external_tech_shock(payload: dict[str, Any]) -> dict[str, Any]:
    """Conservative, deterministic shock classifier with no default-pass on gaps."""
    missing = list(payload.get("missing_gate_fields") or [])
    if missing:
        return {
            "level": "UNKNOWN",
            "score": None,
            "entry_policy": "review_required",
            "position_cap_pct": None,
            "reasons": [f"missing_gate_field:{field}" for field in missing],
        }
    metrics = payload
    score = 0
    reasons: list[str] = []
    thresholds = (
        ("nasdaq_pct", -2.0, 2),
        ("sox_pct", -3.0, 2),
        ("nvidia_pct", -4.0, 1),
        ("korea_tech_pct", -3.0, 1),
        ("korea_market_stress_pct", -4.0, 1),
        ("taiwan_pct", -3.0, 1),
    )
    for field, threshold, weight in thresholds:
        value = to_float(metrics.get(field))
        if value is not None and value <= threshold:
            score += weight
            reasons.append(f"{field}<={threshold:g}:{value:.2f}")
    if payload.get("korea_circuit_breaker") is True:
        score += 4
        reasons.append("verified_korea_circuit_breaker")
    if score >= 6:
        level, cap, policy = "EXTREME", 20, "block_new_tech_entries"
    elif score >= 4:
        level, cap, policy = "HIGH", 35, "tighten_tech_entries"
    elif score >= 2:
        level, cap, policy = "MEDIUM", 50, "reconfirm_tech_entries"
    else:
        level, cap, policy = "LOW", None, "normal"
    return {"level": level, "score": score, "entry_policy": policy, "position_cap_pct": cap, "reasons": reasons or ["no_explicit_external_shock_threshold_hit"]}


def sanity_warnings(quotes: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for quote in quotes:
        pct = to_float(quote.get("pct"))
        price = to_float(quote.get("price"))
        prev_close = to_float(quote.get("prev_close"))
        chart_previous_close = to_float(quote.get("chart_previous_close"))
        key = str(quote.get("key") or "")
        name = str(quote.get("name") or key)
        if pct is None:
            continue
        if price is not None and prev_close is not None and chart_previous_close not in (None, 0):
            chart_pct = (price - chart_previous_close) / chart_previous_close * 100.0
            if abs(chart_pct - pct) >= 1.0:
                warnings.append(
                    f"{name} Yahoo chartPreviousClose为区间基准，已改用日线前一交易日收盘计算"
                )
        if key != "nvidia" and abs(pct) >= 10.0:
            warnings.append(f"{name}日涨跌幅{pct:.2f}%达到极端阈值，建议同花顺/腾讯/新浪二源校验")
        elif key == "nvidia" and abs(pct) >= 15.0:
            warnings.append(f"{name}日涨跌幅{pct:.2f}%达到个股极端阈值，建议二源校验")
    return warnings[:8]


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    trade_date = compact_date(args.trade_date)
    quotes, errors = collect_yahoo_quotes(timeout=args.timeout)
    ths_quotes, ths_errors = collect_ths_quotes(timeout=args.timeout)
    metrics = pct_by_key(quotes)
    ths_metrics = pct_by_key(ths_quotes)
    metrics.update(ths_metrics)
    metrics.update(ths_low_pct_by_key(ths_quotes))
    source_warnings = (
        sanity_warnings(quotes)
        + previous_session_reference_warnings(quotes, ths_quotes)
        + ths_cross_source_warnings(quotes, ths_quotes)
        + downgrade_unconfirmed_korea_tech(metrics, quotes, ths_quotes)
    )[:12]
    flags = derive_risk_flags(metrics, korea_circuit_breaker=bool(args.korea_circuit_breaker))
    status = "ok" if not errors and quotes else ("partial" if quotes else "failed")
    if ths_errors:
        status = "partial" if quotes or ths_quotes else "failed"
    source_quality, missing_gate_fields = assess_external_source_quality(
        metrics,
        errors,
        ths_errors,
    )
    payload: dict[str, Any] = {
        "schema_version": "external_tech_shock_source_v1",
        "trade_date": trade_date,
        "status": status,
        "generated_at": shanghai_now().isoformat(timespec="seconds"),
        "source": "10jqka_v6_line_primary+yahoo_chart_fallback",
        "source_usage": "premarket/intraday tech shock gate; noncritical gaps degrade confidence, critical gaps block tech entries",
        "source_symbols": {spec["key"]: spec["symbol"] for spec in YAHOO_SYMBOLS},
        "ths_source_symbols": {spec["key"]: f"88_{spec['code']}" for spec in THS_SYMBOLS},
        "quotes": quotes,
        "ths_quotes": ths_quotes,
        "errors": errors,
        "ths_errors": ths_errors,
        "source_quality": source_quality,
        "missing_gate_fields": missing_gate_fields,
        "sanity_warnings": source_warnings,
        "notes": [
            "Only tightens tech-sensitive entries under explicit cross-market stress.",
            "Does not create buy signals and does not bypass Strict Gate.",
            "Stale files are ignored by trade_date-scoped loaders.",
        ],
    }
    payload.update(metrics)
    payload.update(flags)
    evidence_dates = sorted({
        compact_date(row.get("trade_date") or row.get("market_time_utc"))
        for row in [*quotes, *ths_quotes]
        if compact_date(row.get("trade_date") or row.get("market_time_utc"))
    })
    latest_evidence_date = evidence_dates[-1] if evidence_dates else ""
    evidence_age_days: int | None = None
    try:
        evidence_age_days = (
            dt.datetime.strptime(trade_date, "%Y%m%d").date()
            - dt.datetime.strptime(latest_evidence_date, "%Y%m%d").date()
        ).days
    except ValueError:
        pass
    payload["evidence_dates"] = evidence_dates
    payload["latest_evidence_date"] = latest_evidence_date or None
    payload["evidence_age_days"] = evidence_age_days
    payload["fresh_for_execution"] = evidence_age_days is not None and 0 <= evidence_age_days <= 4
    payload["external_tech_shock"] = classify_external_tech_shock(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=shanghai_now().strftime("%Y%m%d"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--korea-circuit-breaker", action="store_true")
    parser.add_argument("--no-latest", action="store_true")
    args = parser.parse_args()

    payload = build_payload(args)
    trade_date = compact_date(args.trade_date)
    output = args.out or OUTPUT_DIR / f"external_tech_shock_{trade_date}.json"
    write_json_atomic(output, payload)
    if not args.no_latest:
        write_json_atomic(OUTPUT_DIR / "external_tech_shock_latest.json", payload)
    gate = payload.get("external_tech_shock") or {}
    print(
        "[EXTERNAL-TECH-SHOCK] "
        f"status={payload.get('status')} "
        f"level={gate.get('level')} "
        f"score={gate.get('score')} "
        f"out={output}"
    )
    return 0 if payload.get("status") == "ok" and payload.get("source_quality") == "verified_live" and payload.get("fresh_for_execution") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
