# 数据合同与责任边界

## 1. 输入总合同

`build_premarket_command()` 接收一个 JSON 对象：

```json
{
  "source_trade_date": "YYYYMMDD",
  "execution_trade_date": "YYYYMMDD",
  "market_sentiment": {},
  "major_indices": [],
  "author_ratio": {},
  "external_market": {},
  "sector_cycle": {},
  "topic_context": {}
}
```

完整示例见 `examples/premarket_input.sample.json`。

## 2. `market_sentiment`

必需字段：

- `trade_date`：数据归属交易日。
- `composite_strength`：开盘啦综合强度 0-100。
- `breadth.rise_count/fall_count`。
- `turnover.amount_yi/change_pct`。
- `limit_structure.limit_up_count/limit_down_count`。
- `limit_structure.yesterday_limit_up_return_pct`。
- `limit_structure.yesterday_chain_return_pct`。
- `limit_structure.yesterday_break_return_pct`。

所有字段必须保留 `captured_at`、`source_quality` 和原始 UI 快照路径。解析失败不能复用另一交易日的数字冒充今日。

## 3. `major_indices`

允许两种形态：

1. GM 原始标准化日线：每项带 `name/symbol/bars[]`，引擎自行计算。
2. 已复算技术事实：每项带 `status/trend/close/ma5/ma10/ma20/return_5d_pct/return_20d_pct/volume_ratio_5d`。

推荐第一种，便于回溯。日线复权口径要固定；指数通常无复权争议，但仍需记录 GM SDK 返回口径。

## 4. `author_ratio`

```json
{
  "calibration_only": true,
  "thresholds": {"bottom_watch": 0.3, "negative_effect": 0.6, "balance": 1, "stage_top_watch_1": 1.5, "stage_top_watch_2": 2},
  "observations": [
    {"trade_date": "20260814", "ratio": 1.69, "verification": "ARTICLE_IMAGE_VERIFIED", "source_url": "..."}
  ]
}
```

可接受核验状态：`ARTICLE_TEXT_VERIFIED`、`ARTICLE_IMAGE_VERIFIED`、`CROSS_SOURCE_VERIFIED`、`USER_CONFIRMED`。

采集尝试状态：`NOT_FOUND`、`ARTICLE_FOUND_RATIO_MISSING`、`OCR_AMBIGUOUS`、`AUTHOR_DID_NOT_PUBLISH`、`NON_TRADING_DAY`、`SOURCE_UNAVAILABLE`。这些状态不能进入数值序列。

## 5. `external_market`

必须包含：状态、源质量、冲击级别、理由、每个市场的涨跌幅和市场时间。若关键字段缺失，标记 `PARTIAL/UNAVAILABLE`，不能默认用旧数据。

## 6. `sector_cycle`

每个板块至少包含：

- `sector_code/sector_name`。
- `stage/cycle_day/current_rank`。
- `validation_score/current_main_net_yi`。
- `interval_return_pct/interval_net_yi/net_inflow_days`。
- `intraday_rhythm`。

生命周期状态必须是确定状态：`STARTUP`、`ACCELERATION`、`CLIMAX`、`DIVERGENCE`、`FADE/RETREAT`。原始数据不足时整个板块标记不可用，不使用“待确认”替代生命周期。

## 7. `topic_context`

标题仅用于板块交叉验证和风险复核，不作为事实：

```json
{"status":"OK","headlines":["..."],"risk_headlines":[{"headline":"...","keywords":["减持"]}]}
```

## 8. 输出合同

核心字段：

- `market_emotion`。
- `major_indices/index_summary`。
- `author_long_short_ratio`。
- `internal_swr`。
- `external_resonance`。
- `position_command`。
- `sector_rotation.primary_attack_sectors`。
- `opening_change_triggers`。
- `premarket_disciplines`。
- `source_health`。

`release_status` 初始必须为 `DRAFT_REVIEW_REQUIRED`。DeepSeek 或人工复核后才可成为 `PUBLISHED`。

## 9. 新鲜度与降级

| 数据 | 盘前最大建议时延 | 失效处理 |
| --- | ---: | --- |
| 外围行情 | 30 分钟 | 降低置信度；科技方向不自动放行 |
| 开盘啦情绪/板块 | 20 分钟 | 保留确定性基础仓位，禁止条件扩张 |
| GM 指数日线 | 上一交易日收盘 | 缺 2 个指数则上限压到 35% |
| 作者多空比 | 最近已发布交易日 | 标记断更或待补，不填 0 |
| 题材资讯 | 60 分钟 | 不用于新增主攻，只保留已有生命周期证据 |

每个输出必须留 `evidence_paths` 或等价证据索引，能够追溯原始快照、解析结果和最终合同。
