# 2026-08-17 下一交易日部署与运行勾选表

## 部署前

- [ ] 目标服务器 `git status --porcelain` 已审查，未知改动为 0。
- [ ] 已记录部署前 commit，并建立 `deploy/backup-before-premarket-20260817`。
- [ ] `data/live_signal`、虚拟仓位、飞书队列/outbox、当日 JSONL 已备份到仓库外。
- [ ] 原 GoldMiner 启动适配器和 11 个调度任务仍存在。
- [ ] Python/GM/numpy/pandas 版本已记录，没有盲目升级生产环境。
- [ ] v6 补丁包外层和内部 SHA256 均通过。
- [ ] 作者证据包 SHA256 通过，并用 nightly task 合并账本，未覆盖已有账本。
- [ ] 新 GM、DeepSeek、飞书凭据已轮换；日志和命令行无明文。
- [ ] `A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION` 不为 `1`。

## 代码验收

- [ ] `scripts\validate_repository.ps1` 输出 `AST_OK files=68`、`39 passed`、`REPOSITORY_VALIDATION_OK`。
- [ ] `tools\validate_premarket_package.py` 全部 assertions 为 true。
- [ ] GM 四指数 D-1 冒烟 4/4。
- [ ] GM 全市场/板块 bundle 为 `READY`，errors 为空，覆盖率达标。
- [ ] 现有服务 `startup_self_check()`、Tick 订阅、11 个任务、飞书启动回执正常。

## 08:35-08:55

- [ ] GoldMiner 终端已登录，同用户、同桌面、同权限级别。
- [ ] 作者账本最新日 `20260814`，值为 1.69；上一交易日为 2.88。
- [ ] 运行 `Invoke-PremarketCommand.ps1 -SourceTradeDate 20260814 -ExecutionTradeDate 20260817 -RunDeepSeek`。
- [ ] GM 原始数据、外围、normalized、draft、DeepSeek 四份证据全部生成。
- [ ] `source_health.missing=[]` 且 `stale_or_undated=[]`；否则保持草稿并报警。
- [ ] DeepSeek 没有提高仓位、没有新增板块。
- [ ] 未传 `-PublishIfEligible`；最终仍为 `REVIEW_PENDING` 是当前正确结果。

## 09:20

- [ ] GM 竞价 tick 日期为 `20260817`，时间在 09:15-09:25，原始 CSV.gz 可追溯。
- [ ] 只生成 opening candidate，不使用 reviewed/draft 冒充正式 `PUBLISHED`。
- [ ] opening candidate 仓位不高于基线，板块是原集合子集。
- [ ] 只有完成全部影子检查并有日志证据后，才运行 acceptance recorder。
- [ ] `real_orders_sent=false`，原策略订单/信号/状态未被盘前指挥台改变。

## 盘后

- [ ] 核对最终飞书事件、outbox、JSONL 和状态文件，不只看进程存活。
- [ ] 保存当天 reviewed、health、GM opening、candidate、日志和 SHA256。
- [ ] 运行 release gate，真实计数应从 0/5 增至 1/5；若无完整证据则保持 0/5。
- [ ] 记录所有 P0/P1/P2 问题和下一日修复动作。

## 当日结论

- [ ] 原观察池策略稳定运行。
- [ ] 盘前指挥台完成影子验证。
- [ ] 没有真实订单由盘前指挥台产生。
- [ ] 未达到 20+5+5，仍未授权正式消费。
