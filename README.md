# A股观察池每日盯盘信号策略

本仓库用于统一管理从原服务器导入的 A 股观察池每日盯盘信号策略、后续补丁和部署版本。

当前导入版本来自 2026-08-14 的 V17 源码包。系统以信号、解释、飞书投递和虚拟信号台账为主要输出；`live_signal_service.py` 本身不下单。`rotation_strategy.py` 是独立的日线研究/回测适配器，包含默认关闭的订单路径，不属于 V17 实时信号服务启动入口。

## 当前状态

- 已导入 43 个 Python 源文件和 3 个观察池/分类文件。
- 原始 ZIP 与解压源码已核对一致，哈希见 `docs/IMPORT_MANIFEST.md`。
- 实时服务仍依赖原服务器的 `D:\codex\a_share_rotation` 数据目录。
- 当前源码包缺少把 `LiveSignalService` 接入 GoldMiner 回调和 11 个调度任务的实时启动适配器。
- 因此本仓库当前是“已纳管、待补齐运行入口”，不能标记为可直接部署。
- 已引入独立的 `src/premarket_command` 盘前指挥层；证据不全、日期不明、DeepSeek 不可用或验收期不足时均不能发布。

## 目录

```text
src/                         策略、实时引擎、研究与回放脚本
universe/                    固定观察池、自研池与稳定分类
tests/                       确定性回归测试
docs/IMPORT_MANIFEST.md      原包来源、哈希与导入边界
docs/DEPLOYMENT_GATES.md     原服务器部署前强制门禁
docs/premarket_command/      盘前指挥台逻辑、Windows部署、运行手册和当前落地状态
scripts/validate_repository.ps1  本地统一验证入口
scripts/Invoke-PremarketCommand.ps1  Windows 盘前采集/标准化/复核一键入口
```

## 本地验证

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\scripts\validate_repository.ps1
python .\tools\validate_premarket_package.py
```

`requirements.txt` 记录本机已经通过导入检查的版本。部署到原服务器前，必须先导出并对比服务器实际 Python、GoldMiner SDK、`numpy` 和 `pandas` 版本，不得直接覆盖生产环境。

## Git 补丁流程

1. `main` 保存已验收的基线和合并补丁。
2. 每个需求使用独立分支，例如 `patch/20260815-runtime-root`。
3. 只暂存本次补丁文件，运行 `git diff --cached --check`。
4. 执行 `scripts/validate_repository.ps1` 和本需求的目标回归。
5. 通过 Pull Request 审查后合并，并为原服务器部署提交打标签。
6. 原服务器只部署明确的 commit/tag，不直接复制未提交工作区。

## 运行边界

- 飞书 Webhook、GoldMiner Token、DeepSeek Key 只能通过安全环境变量注入，禁止提交。
- 建议点源运行 `scripts/Set-PremarketSecrets.ps1` 以隐藏输入；聊天中曾暴露的密钥应在部署前轮换。
- `data/live_signal`、分钟缓存、JSONL证据和虚拟信号台账属于运行状态，禁止纳入 Git，也不能在代码回滚时覆盖。
- `A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION` 必须保持关闭，除非另有经过审批和完整回归的订单执行任务。
- 信号虚拟台账不是券商真实持仓，不能推导真实可卖数量。
