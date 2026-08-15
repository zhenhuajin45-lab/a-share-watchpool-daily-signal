# Windows + 掘金 + 开盘啦部署方案

## 1. 推荐目录

```text
C:\a_share_premarket_command\
  adapters\
  config\
  data\raw\gm\
  data\raw\kaipanla\
  data\normalized\
  reports\
  logs\
  src\premarket_command\
  tools\
```

Python 建议使用与本地 GM SDK 兼容的版本，不要为了本包强制升级 GM 的 Python 环境。

## 2. GM SDK

1. 在 Windows 用户环境变量配置 `GM_TOKEN`，不要写入 JSON、日志或 Git。
2. 先运行：

```powershell
python adapters\gm_market_data_adapter.py --trade-date 20260814 --output data\raw\gm\20260814.json
python adapters\gm_market_breadth_sector_adapter.py --trade-date 20260814 --output data\raw\gm_market_sector\20260814.bundle.json --evidence-dir data\raw\gm_market_sector --include-concepts
```

3. 核对四个指数各有至少 30 根日线，推荐 80 根。
4. 如果本机 GM SDK 的 `history_n` 字段或指数代码不同，只修改适配层，不修改确定性引擎。
5. 此适配器没有订单接口，能够与现有 GM 策略隔离部署。

## 3. 开盘啦 Windows UIA

开盘啦桌面端不是稳定公开 API，Windows 端使用只读 UI Automation：

```powershell
pip install -r requirements.txt
python adapters\kaipanla_windows_uia_capture.py `
  --page-label market_emotion `
  --output data\raw\kaipanla\market_emotion.json `
  --screenshot data\raw\kaipanla\market_emotion.png
```

首次实施需要 Windows Codex 完成页面标定：

- 市场情绪页：综合强度、涨跌家数、量能、涨跌停和昨日强势样本反馈。
- 板块页：板块强度、排名、主力净额、量能/成交。
- 区间统计页：5 日和 10 日强度、涨幅、净额、成交、主力买卖、净流入天数。
- “明天炒什么”：最新文章日期、标题、风险词。

原始 UIA 文本必须先落盘，再解析为交叉证据合同。不要让页面抓取代码直接修改仓位；GM 全市场复算是情绪和板块的主源。

若 UIA 读不到表格：检查开盘啦与 Python 是否同一权限级别、窗口是否可见、Windows UI Automation 是否暴露子元素。OCR 只能做备份源，OCR 结果必须带截图和置信度。

## 4. 公众号作者多空比

纯 Python 抓取微信公众号不稳定，推荐由 Windows Codex 的浏览器自动任务在每个交易日约 23:10 执行：

1. 查找“橙先生的视界”最新文章及新浪、东方财富、搜狐等转载。
2. 核对发布日期、作者、图片标题和“今日/上个交易日”标签。
3. 图中只有曲线而没有明确数值时，不估算。
4. 用 `adapters/author_ratio_ledger.py` 写入证据账本。
5. 作者未发布时登记 `AUTHOR_DID_NOT_PUBLISH`。
6. 同日新值和已有值冲突时进入隔离，不静默覆盖。

示例：

```powershell
python adapters\author_ratio_ledger.py `
  --ledger data\normalized\author_ratio.json `
  --trade-date 20260814 --ratio 1.69 `
  --verification ARTICLE_IMAGE_VERIFIED `
  --source-url "公众号文章URL" `
  --evidence-text "图片中明确标注今日1.69，上个交易日2.88"
```

## 5. 外围行情

优先顺序：本地行情平台/同花顺实时源 -> 第二行情源交叉校验 -> 公共源降级。每个市场必须使用其自身前一交易日收盘，不得把盘中某个时点误作前收。

关键极端值应二源复核。韩国 KOSPI 与 KOSDAQ 不能互相替代；“熔断”必须有明确状态证据。

## 6. 合同构建与 DeepSeek

推荐用一键脚本采集、标准化和构建：

```powershell
. .\scripts\Set-PremarketSecrets.ps1
.\scripts\Invoke-PremarketCommand.ps1 -SourceTradeDate 20260814 -ExecutionTradeDate 20260817 -RunDeepSeek
```

执行日 09:20 使用 GM 集合竞价 tick 生成只收紧复核：

```powershell
.\scripts\Invoke-PremarketOpeningReview.ps1 -ExecutionTradeDate 20260817 -PublishedContract data\premarket_command\published\latest.json -PreviousGmBundle data\raw\gm_market_sector\20260814.bundle.json
```

GM `current(..., include_call_auction=True)` 的快照日期必须等于执行日；周末、节假日或旧快照会标记 `UNAVAILABLE`，保留基础上限但取消条件扩张。

只有最终文件的 `release_status=PUBLISHED` 才允许被其它策略读取。DeepSeek 不可用时保持 `REVIEW_PENDING`；不能让 API 故障自动变成看空，也不能手工跳过审核与 `20+5+5` 门槛。

## 7. 与其它策略集成

每个策略只读取以下字段：

- `position_command.base_cap_pct`：策略总仓不得超过此值。
- `position_command.conditional_expansion_cap_pct`：仅盘中满足全部条件后可用。
- `sector_rotation.primary_attack_sectors`：方向白名单，不是股票白名单。
- `opening_change_triggers`：开盘突变降级条件。

策略自己的股票池、信号、止损、T+1、涨跌停、订单和账户风控保持独立。盘前指挥台只能收紧下游策略权限，不能绕过其门控。

## 8. 安全顺序

离线样例 -> GM 数据 DRY_RUN -> 只生成盘前合同 -> 多策略影子读取 -> 模拟盘 -> 人工审核 -> 再考虑实盘。不得把本包直接接到订单函数。
