# 盘前指挥台一页故障手册

1. 先运行 `powershell -ExecutionPolicy Bypass -File .\scripts\validate_repository.ps1`。失败时不发布。
2. GM：运行 `python adapters\gm_market_data_adapter.py --trade-date YYYYMMDD --output data\raw\gm\YYYYMMDD.json`。退出码 2 或 `ready=false` 时启动/登录 GoldMiner、核对同权限级别和 `GM_TOKEN`；禁止用旧指数冒充 D-1。
3. 开盘啦：每页运行只读采集并保留 JSON 与 PNG。`SCREENSHOT_ONLY` 表示当前 Android 容器不暴露 UIA 文本，只能人工/OCR 双检；解析器停用，不填猜测值。
4. 作者多空比：23:10 证据 JSON 只有 `ARTICLE_*_VERIFIED`/`CROSS_SOURCE_VERIFIED`/`USER_CONFIRMED` 才入序列。断更、未找到、OCR 模糊和冲突只记 attempts，不补 0。
5. 外围：`verified_live` 才满足发布源门；KOSPI 与 KOSDAQ 分栏。`--korea-circuit-breaker` 仅在已有真实熔断证据时使用，大跌幅本身不等于熔断。
6. DeepSeek：必须同时保留 `.prompt.txt`、`.raw.json`、review JSON 和 final JSON。401、超时、非 JSON、Key 缺失均为 `REVIEW_PENDING`。
7. 09:20：运行 `tools\run_premarket_opening_review.py`；输出仓位不得高于盘前发布值，板块不得超出原白名单。缺源取消扩张并报警。
8. 飞书：仅 `PUBLISHED` 显示可用摘要；状态为 `NOT_FOUND`/`NOT_PUBLISHED` 时现有每日计划继续独立运行，不放宽门控。
9. 回滚：切回已标记 Git commit/tag；不要覆盖服务器 `data/`、日志、账本、飞书 outbox 或虚拟仓位状态。
10. 升级为正式消费前运行 `tools\evaluate_premarket_release_gate.py`，必须得到 `release_gate=MET`；任何未通过项都不得接真实订单函数。
