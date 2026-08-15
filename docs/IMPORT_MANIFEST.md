# V17 原包导入清单

## 来源

- 源码包标注日期：2026-08-14
- 本机纳管日期：2026-08-15
- 原始说明书：`A股短线板块轮动策略通俗说明书_V17.md`
- 初始 Git 提交：`3c2a542` (`Import V17 source package`)

## 原始运输包哈希

| 文件 | SHA-256 |
|---|---|
| `src.zip` | `0DEDC657A2834DDA18180DBC854D9DD2C60E93BDEAD94834F90FC8CCEA7F6402` |
| `universe.zip` | `37F73BFEEC6F759E9583A130BBCC4A343E09A9ED4D194B8B28E628CFD87C02B8` |

两个 ZIP 保留在本地作为原始运输证据，但因内容与解压源码重复且包含 `__pycache__`，不进入常规 Git 历史。需要长期归档时应作为 GitHub Release 附件保存，并再次核对上述哈希。

## 一致性检查

- `src.zip` 中排除 `__pycache__` 后共有 43 个 Python 文件。
- 本地 `src/` 共有 43 个 Python 文件。
- ZIP 与本地源码逐文件 SHA-256 一致，无缺失、无多余、无内容差异。
- `universe.zip` 与本地 `universe/` 的 3 个文件逐文件一致。
- 43 个 Python 文件共 19,658 行，全部通过 Python AST 解析。

## 未包含的运行资产

以下内容不在导入包中，也不应从 GitHub 仓库推断：

- 原服务器 `D:\codex\a_share_rotation\data` 下的日线、分钟和实时证据。
- `virtual_signal_positions.json`、飞书队列/outbox及当日JSONL证据。
- 原服务器 GoldMiner 项目配置、Token、任务调度与进程托管方式。
- DeepSeek Key 和飞书 Webhook。
- 将 `LiveSignalService` 注册到 GoldMiner Tick/调度回调的实时启动文件。
