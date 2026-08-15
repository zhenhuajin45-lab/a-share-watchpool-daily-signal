# 原服务器部署门禁

本文件定义补丁合并或回到原服务器前必须满足的最低条件。门禁未通过时只能标记为研究或待部署，不能宣称生产就绪。

## 1. 版本与差异

- 部署来源必须是已推送的明确 commit 或 tag。
- 必须保存目标版本、当前服务器版本和回滚版本的 commit SHA。
- 必须人工查看本次完整 diff，并运行 `git diff --check`。
- 策略阈值、股票池、调度和副作用变化必须写入 `CHANGELOG.md`。

## 2. 环境与配置

- 记录原服务器 Python、`gm`、`numpy`、`pandas` 版本。
- 验证实际数据根目录、GoldMiner Token、飞书 Webhook 和 DeepSeek Key 可用，但不得在日志中输出密钥值。
- 确认 `A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION` 未被意外设置为 `1`。
- 运行路径必须显式区分实时信号服务与 `rotation_strategy.py` 研究/回测路径。

## 3. 实时链路

- 验证 GoldMiner 实时适配器已经注册 Tick 订阅、动态订阅/退订回调和 11 个调度任务。
- 启动后必须执行 `startup_self_check()`，不能把 `bootstrap()` 返回空字典当成启动成功。
- 盘前日线必须是 `ADJUST_PREV` 且严格截止 D-1；分钟指标只能使用已完成K线。
- 跨交易日迟到 Tick、午休 Tick 和 15:00 后 Tick 必须被拒绝。
- 飞书正式模式必须收到启动回执，队列工作线程和业务响应码检查必须正常。

## 4. 状态与T+1

- 部署前备份 `data/live_signal`，至少包括：
  - `virtual_signal_positions.json`
  - `feishu_delivery_queue.jsonl`
  - `feishu_outbox.jsonl`
  - 当日 `tick_samples.jsonl`
- 代码部署或回滚不得删除、清空或用仓库内容覆盖这些文件。
- 当日信号入场遇到卖点时，行动层必须输出T+1锁定；前一交易日信号仓位才可输出减仓/卖出。
- 必须明确虚拟台账不是券商真实持仓，不得伪造 `sellable_qty`。

## 5. 验证与回滚

- 本地统一验证脚本必须通过。
- 每个补丁必须有能复现该问题的目标回归；只运行语法检查不够。
- 上线后核对最终飞书事件、JSONL证据和状态文件，而不是只看进程存活。
- 回滚方案必须同时说明代码回滚和状态兼容方式。

## 当前未通过项

- 实时 GoldMiner 启动适配器不在导入包中。
- 运行根目录仍硬编码为 `D:\codex\a_share_rotation`。
- 尚未取得原服务器依赖版本、调度配置、运行日志和状态文件快照。
