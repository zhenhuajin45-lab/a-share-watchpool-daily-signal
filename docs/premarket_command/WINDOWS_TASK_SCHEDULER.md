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

生产服务器落地时使用“无论用户是否登录都运行”、最高权限与 GoldMiner/开盘啦保持相同账户和权限级别；启用“错过后尽快运行”，禁止并行实例。任务定义应导出 XML 并随部署 Evidence Pack 保存，但 XML 中不得出现凭据。节假日判断必须使用 GM 交易日历，不用工作日近似。
