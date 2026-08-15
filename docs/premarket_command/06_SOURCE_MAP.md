# 成熟代码映射与复用建议

`reference_source/` 是当前 Mac 项目在 2026-08-14 的只读参考快照。Windows Codex 应按模块复用逻辑，不应把文件原样覆盖到 Windows 工程。

| 参考文件 | 成熟能力 | Windows 处理 |
| --- | --- | --- |
| `market_long_short_ratio.py` | SWR 分项、权重、作者周期融合 | 核心公式已抽入 `src/premarket_command/engine.py` |
| `build_premarket_decision_contract.py` | 情绪、指数、仓位取最小值、主攻方向 | 已去掉股票计划池后抽入通用引擎 |
| `run_premarket_deepseek_review.py` | DeepSeek 反方复核和 tighten-only | 已抽入 `review.py` 和工具脚本 |
| `track_author_long_short_ratio.py` | 证据状态、冲突隔离、断更/缺失分离 | Windows 使用精简账本适配器，完整逻辑可继续移植 |
| `research_author_long_short_ratio.py` | 作者公式复刻研究和样本外验收 | 仅研究，不参与门控；可后续迁移 |
| `update_kaipanla_market_context.py` | 情绪和“明天炒什么”文本解析 | 解析思想复用，采集层改为 Windows UIA |
| `update_kaipanla_sector_snapshot.py` | 板块强度、区间统计、二波验证、生命周期 | 核心参考；Windows 需先完成 UIA 字段标定 |
| `fetch_external_tech_shock.py` | 外围前收口径、韩国科技校验、冲击等级 | 可直接迁移大部分纯 Python 逻辑，行情源按 Windows 环境替换 |

参考样本包括 8 月 14 日情绪、板块周期、外围数据、作者序列以及 8 月 17 日盘前合同和 DeepSeek 结果，可用于 Windows 端回归测试。

## 不应迁移的耦合

- `active_plan_v2_for_trading.json` 和股票计划池。
- Mac 的 Swift Accessibility bridge 和 macOS 权限配置。
- 本机模拟盘账户、订单、持仓和飞书凭据。
- 任何 API Key、Webhook、GM Token、Cookie 或登录态。
- 当前 Web 工作台渲染代码；Windows 可先只产 JSON，验证后再接 UI。

## 推荐分层

```text
collectors/       GM、开盘啦、公众号、外围的原始证据
normalizers/      转为稳定数据合同
premarket_core/   本包确定性引擎
review/           DeepSeek/人工复核
publisher/        原子发布 reviewed contract
consumers/        其它策略只读消费
```

各层只能向下游传 JSON 合同，禁止采集器直接修改策略仓位或订单状态。
