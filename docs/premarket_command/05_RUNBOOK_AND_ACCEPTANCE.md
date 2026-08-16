# 日常运行、异常处理与验收

## 1. 每日节奏

### 前一交易日盘后

- 15:10 固化 GM 指数和市场日线。
- 18:00 完成各策略盘后复盘。
- 23:10 抓取公众号作者多空比，写入证据账本；断更单独记录。

### 执行日盘前

- 08:35 GM SDK 健康检查并加载四大指数、全 A 股日线、历史涨跌停价、申万行业/概念和资金流。
- 08:40 刷新外围市场，极端值二源复核。
- 08:45 可选抓取开盘啦四页作为交叉证据；失败不得补造数值。
- 08:52 构建确定性盘前指挥合同。
- 08:55 DeepSeek 全维反方质检并发布最终合同。
- 09:20 重新抓取竞价、情绪、外围和板块排名，计算状态差量。
- 09:25 后各策略自行执行个股信号与风控。

周末或节假日不生成伪交易日数据。盘前合同的 `source_trade_date` 是最近已收盘交易日，`execution_trade_date` 是下一交易日。

## 2. 09:20 状态突变

下列任一项只允许收紧：

- 主要指数竞价与隔夜判断显著反向。
- 外围冲击由 LOW/MEDIUM 升至 HIGH/EXTREME。
- GM 竞价/市场复核显著转弱；开盘啦仅可提供收紧用交叉证据。
- 主攻板块跌出前 12、主力净额转负、盘中节奏转为 DISTRIBUTING/FADING。
- 突发利空经过事实核验。

若数据源在 09:20 缺失，取消条件扩张并报警，不把缺失当空头票。

## 3. 异常处理

| 异常 | 处理 |
| --- | --- |
| GM SDK 无法连接终端 | 不发布指数完整合同；记录健康错误 |
| 开盘啦 UIA 结构变化 | 保存原始文本/截图，停用解析器，禁止沿用错误数字；GM 主源继续独立判断 |
| 公众号断更 | `AUTHOR_DID_NOT_PUBLISH`，沿用最近核验值并标明日期 |
| OCR 数字不清 | `OCR_AMBIGUOUS`，不入库 |
| DeepSeek 401/超时 | `REVIEW_PENDING`，等待人工或接口恢复 |
| 外围单源极端值 | 二源复核前不升级为熔断/极端冲击 |
| 板块数据缺失 | 不新增主攻，保留基础市场判断 |

## 4. 离线验收

运行：

```powershell
python tools\validate_premarket_package.py
```

必须通过：

- 输出没有股票计划池。
- DeepSeek 无法提高仓位。
- DeepSeek 无法新增板块。
- 仓位在 0-100%。
- 数据源健康状态存在。
- 单元测试全部通过。

## 5. Windows 实施验收

### 数据

- 连续 10 个交易日四大指数完整，日期无错位。
- GM 全市场日线与行业覆盖率分别至少 90%，原始 CSV 哈希可追溯；开盘啦若启用，同屏数字与交叉证据 JSON 抽查一致率 100%。
- 作者多空比已核验值、断更日、冲突记录可追溯。
- 外围数据前收基准与第二行情源抽查一致。

### 逻辑

- 用至少 20 个历史交易日回放盘前合同，无未来数据。
- 情绪、SWR、板块生命周期的输入与输出逐项可解释。
- 主攻板块只来自启动/加速且综合分达标。
- 缺失数据降低置信度，不产生伪空头或伪多头。

### 集成

- 至少 5 个交易日影子运行：各策略读取合同但不改变订单。
- 再运行 5 个交易日模拟盘：确认仓位上限和方向白名单只收紧权限。
- 检查每个策略对 `REVIEW_PENDING`、过期合同和日期不匹配的拒绝行为。

每个交易日先保存合同、健康报告、09:20 快照或模拟盘日志，再用 `tools/record_premarket_acceptance.py` 生成带 SHA-256 的阶段证据。脚本要求显式列出该阶段全部检查；影子和模拟盘还必须传 `--confirm-no-real-orders`。例如影子阶段：

```powershell
python tools\record_premarket_acceptance.py --stage shadow --execution-date 20260817 `
  --check completed --check read_only --check orders_unchanged `
  --check stale_contract_rejected --check date_mismatch_rejected --check review_pending_rejected `
  --evidence-file reports\premarket_command\premarket_command.20260817.reviewed.json `
  --evidence-file reports\premarket_command\gm_opening.20260817.json `
  --confirm-no-real-orders --output data\acceptance\shadow\20260817.json
```

记录后运行 `tools/evaluate_premarket_release_gate.py`；无效文件不会计数，并会列入 `evidence_quality.invalid_evidence`。

## 6. 发布门槛

只有同时满足以下条件才允许多个策略正式消费：

- `release_status=PUBLISHED`。
- `execution_trade_date` 与策略交易日一致。
- 关键数据未过期。
- DeepSeek 严格 JSON 审核为确认或带限制确认。
- `20` 日回放、`5` 日影子和 `5` 日模拟盘门槛结构完整且全部通过。
- 09:20 差量复核未触发更严格合同。

本系统提供纪律和风险上限，不构成收益承诺或投资建议。
