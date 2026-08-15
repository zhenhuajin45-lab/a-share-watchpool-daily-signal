# coding: utf-8
"""发送量能软因子闭环测试；不启动行情、不修改虚拟持仓。"""

from __future__ import annotations

import json
from datetime import datetime

from live_signal_service import FeishuNotifier


def main() -> None:
    now = datetime.now()
    message = "\n".join([
        "【A股轮动｜量能软因子闭环测试｜TEST_ONLY｜中文修正版】",
        f"发送时间：{now:%Y-%m-%d %H:%M:%S}；证据截面：2026-08-10已完成日线（前复权）",
        "真实案例：中国稀土（000831）｜稀土分离及稀土氧化物",
        "",
        "一、盘前先验",
        "日线路线：趋势延续｜基础强度82/100｜KDJ(9,20,2) J=58.7、慢线向上｜MACD(5,10,5)改善",
        "月线：9,20,2 J=52.0；8,2,2 J=55.2｜双周期支持；板块置信度偏低，仍需盘中确认。",
        "",
        "二、量能辅助",
        "命中条件A：最近两日均为红K且站上各自5日均量；量/5日均量=1.67、1.79。",
        "条件B未命中：逐日放量+51.4%、+48.1%，三日总量+124.2%，超过温和放量上限。",
        "量能加分：+5（只用于候选排序）｜候选排序分87/100；没有修改原始日线动作。",
        "",
        "三、现在该怎么做",
        "空仓：[等待实时买点] 不是现在直接买入；等待集合竞价、稀土板块梯队、VWAP承接，以及5/15/30分钟KDJ(8,2,2)+MACD(5,10,5)确认。",
        "已有仓位：[继续持有观察] 盘中承接确认后可参考做T事件；结构转弱再按减仓/卖出事件处理。",
        "风险：J值接近60保护区且近期放量较快，不能追价；高开不机械否决，但必须验证可成交性和回踩承接。",
        "",
        "四、反例校验",
        "晋控电力（000767）同时满足两项量能条件、潜在+7，但MACD未改善、月线偏弱、日线资格不足，动作仍为WAIT。量能不能独立制造买点。",
        "",
        "闭环：D-1日线先验 → 量能软排序 → 行动边界 → 竞价/板块/分钟/Tick确认 → 新开仓、持有/做T或退出 → T+1约束。",
        "本消息只测试规则解释与飞书投递，不写虚拟持仓、不发送订单，也不是当前买入建议。",
    ])
    event_id = f"manual_volume_closure_utf8_fix:{now:%Y-%m-%dT%H:%M:%S}"
    result = FeishuNotifier().send_text(message, event_id)
    print(json.dumps({
        "event_id": event_id,
        "ok": bool(result.get("ok")),
        "response_code": (result.get("response") or {}).get("code"),
        "source_contains_replacement_question_mark": "?" in message,
        "source_first_line_unicode_escape": message.splitlines()[0].encode("unicode_escape").decode("ascii"),
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
