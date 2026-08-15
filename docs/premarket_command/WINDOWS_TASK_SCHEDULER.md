# Windows 任务计划审计配置

任务使用运行账户的安全环境变量，不在 XML、参数或日志中写入 `GM_TOKEN`、`DEEPSEEK_API_KEY`、飞书 Webhook。

| 计划时间 | 动作 | 成功条件 |
| --- | --- | --- |
| 23:10（交易日） | 浏览器证据任务生成作者证据 JSON，再运行 `tools\author_ratio_nightly_task.py` | 已核验值入 observations；其它情况只记 attempt |
| 08:35 | `adapters\gm_market_data_adapter.py` + `gm_market_breadth_sector_adapter.py` | 四指数及全市场/板块均 `ready=true`，最后交易日为 D-1 |
| 08:40 | `adapters\external_market_adapter.py` | `status=ok` 且 `source_quality=verified_live` |
| 08:45 | 开盘啦四页只读交叉证据（可选） | 原始 JSON/PNG 可审计；失败不补数、不授予权限 |
| 08:52 | `tools\build_premarket_command.py` | `READY_FOR_DEEPSEEK_REVIEW` |
| 08:55 | `tools\run_premarket_deepseek_review.py` | 四份 DeepSeek 证据齐全；未达到 `20+5+5` 时 final 必须保持 `REVIEW_PENDING` |
| 09:20 | `scripts\Invoke-PremarketOpeningReview.ps1` | GM 竞价快照有原始 tick 哈希；审计结果为 `UNCHANGED` 或 `TIGHTENED` |

生产服务器落地时，GM 终端和开盘啦 UIA 任务必须使用桌面应用当前登录用户，并选择“仅当用户登录时运行”。任务进程与桌面应用保持相同完整性级别；不要无条件勾选最高权限，否则普通权限运行的开盘啦可能无法被 UIA 读取。只有完全不依赖桌面会话的纯文件任务才可评估“无论用户是否登录都运行”。

统一设置 `Start in` 为实际项目根目录，禁止并行实例；失败最多重试 1 次、间隔 2 分钟。启用“错过后尽快运行”前必须先使用 GM 交易日历判断当天是否为交易日，不能在周末/节假日补生成伪合同。任务定义应导出 XML 并随部署 Evidence Pack 保存，导出后扫描 XML，确保没有 Token、API Key、Webhook 或明文密码。

首次部署的下一交易日建议人工监控，不直接无人值守。详细部署参数、08:35 主链和 09:20 影子流程见 `07_OTHER_ENVIRONMENT_HANDOVER.md` 与 `08_NEXT_TRADING_DAY_CHECKLIST.md`。
