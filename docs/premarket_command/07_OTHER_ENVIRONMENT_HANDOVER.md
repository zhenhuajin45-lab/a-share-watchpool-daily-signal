# A股多策略盘前指挥台：其它 Windows/掘金环境补丁交接与部署

## 1. 交接结论与适用范围

本交接用于把 `agent/premarket-command` 分支部署到保存原 V17 观察池策略的 Windows/掘金环境。部署目标是新增一个只读、策略中立的盘前指挥层，并把已发布合同摘要接入现有盘前飞书消息。

本补丁不替换原服务器的 GoldMiner 实时启动适配器，不修改账户、订单、T+1、个股信号、止损或股票池。`src/live_signal_service.py` 只新增读取 `PUBLISHED` 合同和飞书展示；合同不存在、未发布、过期或日期不匹配时，原每日计划继续独立运行，不放宽任何门控。

当前真实状态是“可在下一交易日进行盘前采集和影子运行”，不是“已允许正式策略消费”：`20+5+5` 仍为 0/20、0/5、0/5，`release_gate=NOT_MET`，因此不得制造或手工复制 `data/premarket_command/published/latest.json`。

## 2. 版本与运输物

推荐来源：

- GitHub 分支：`agent/premarket-command`
- Draft PR：`https://github.com/zhenhuajin45-lab/a-share-watchpool-daily-signal/pull/2`
- 兼容基线：`main` 的 `46cc10f`（`Merge pull request #1 ... repository-baseline`）
- 代码补丁提交顺序：
  1. `bacbd5e` `feat: add guarded premarket command layer`
  2. `be5ccbc` `feat: use GM as premarket market and sector source`
  3. `c0cccbb` `feat: ingest verified author ratio evidence`
  4. `efa5133` `fix: harden premarket release evidence gate`
  5. `4c27fa4` `feat: record auditable premarket acceptance evidence`
  6. 本交接文档提交

离线运输物由交付机 `reports/` 目录提供：

- `premarket_command_patch_bundle_v6.zip`：最终顺序补丁与内部 `SHA256SUMS.txt`。
- `premarket_command_patch_bundle_v6.zip.sha256`：补丁包外层校验和。
- `deployment_evidence_20260815/author_ratio_20260814_evidence_v1.zip`：作者截图、证据清单和参考账本。
- `author_ratio_20260814_evidence_v1.zip.sha256`：作者证据包校验和。

只使用最终 v6；v3/v4/v5 是迭代历史，不用于目标服务器部署。

## 3. 补丁内容审计

| 顺序 | 主要内容 | 既有文件影响 | 风险边界 |
| --- | --- | --- | --- |
| 1 | 指挥台引擎、合同、DeepSeek 限权、发布器、开盘复核、基础适配器 | `live_signal_service.py` 增加只读合同摘要和飞书行；README/requirements 更新 | 不调用订单 API；非 `PUBLISHED` 合同被拒绝 |
| 2 | GM 四指数、全市场情绪、板块轮动、09:20 竞价、Windows 一键脚本 | 新增适配器/脚本并强化发布门 | GM 是可复算主源；开盘啦仅交叉证据 |
| 3 | 公众号同文多日值、图片 SHA256、外围执行日新鲜度修复 | 作者任务和引擎小范围修改 | 整批预检查；冲突、旧图、错日期不入账 |
| 4 | `premarket_release_gate_v2` | 发布门与测试修改 | 空壳 JSON 不计数；无效证据阻断发布 |
| 5 | 验收证据生成器 | 新增工具、测试、运行文档 | 影子/模拟必须显式零真实订单并绑定证据哈希 |
| 6 | 其它环境交接和下一交易日运行文档 | 文档/清单 | 不改变运行逻辑 |

补丁审计结果：按顺序 `git am --3way` 试装成功；试装 tree 与源分支 tree 完全一致；试装副本 `scripts/validate_repository.ps1` 通过，39 项测试通过。补丁中没有二进制内容或硬编码 Token/API Key。

## 4. 部署前硬门禁

以下任一项不满足，停止部署，不带病进入交易日：

1. 已停止旧进程或确认部署只更新未被占用的源码；没有第二个任务实例并行运行。
2. `git status --porcelain` 为空；若不为空，先提交/归档服务器本地改动，不得强行覆盖。
3. 已记录服务器当前 commit、Python、`gm`、`numpy`、`pandas`、运行账户和 GoldMiner 终端版本。
4. 已把 `data/live_signal`、飞书队列/outbox、虚拟信号仓位和当日 JSONL 备份到仓库外目录。
5. 原服务器的 GoldMiner Tick 订阅和 11 个调度任务启动文件仍在；本仓库没有该启动适配器，补丁不能替代它。
6. 实际项目根目录与原代码约定一致，默认是 `D:\codex\a_share_rotation`。若不是该目录，必须在上线前完成路径审计，不能只改一个文件后假定全部脚本可用。
7. `A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION` 未设置为 `1`。
8. GM、DeepSeek、飞书密钥已轮换；聊天中出现过的旧值不能继续使用。
9. 任务运行账户与 GoldMiner/开盘啦桌面进程是同一 Windows 用户、同一交互桌面和同一权限级别。

部署前采集环境快照：

```powershell
Set-Location 'D:\codex\a_share_rotation'
git status -sb
git rev-parse HEAD
python --version
python -m pip show gm numpy pandas pytest pywinauto
Get-Process | Where-Object { $_.ProcessName -match 'gold|gm|kaipanla|开盘啦' } |
  Select-Object ProcessName,Id,SessionId,Path
'GM_TOKEN','DEEPSEEK_API_KEY','A_SHARE_ROTATION_FEISHU_WEBHOOK_URL' | ForEach-Object {
    [PSCustomObject]@{
        Name = $_
        Present = [bool][Environment]::GetEnvironmentVariable($_, 'Process')
    }
}
```

最后一条只检查是否存在，不输出值。不要用 `set`、`Get-ChildItem Env:` 全量打印环境变量。

## 5. 运行状态备份

备份必须放在仓库外，防止 Git 操作或脚本误覆盖：

```powershell
$projectRoot = [IO.Path]::GetFullPath('D:\codex\a_share_rotation')
$backupRoot = [IO.Path]::GetFullPath('D:\strategy_backups\a_share_rotation_20260816_before_premarket')
if ($backupRoot.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup directory must be outside project root'
}
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
Copy-Item -LiteralPath "$projectRoot\data\live_signal" -Destination "$backupRoot\live_signal" -Recurse
git -C $projectRoot rev-parse HEAD | Set-Content -LiteralPath "$backupRoot\pre_deploy_commit.txt" -Encoding UTF8
python -m pip freeze | Set-Content -LiteralPath "$backupRoot\pip_freeze.txt" -Encoding UTF8
```

如果某个状态目录不存在，先核对真实运行根目录，不要临时创建空目录冒充备份成功。

## 6. 推荐部署方式：从 GitHub 建部署分支

目标服务器已有同一仓库时：

```powershell
Set-Location 'D:\codex\a_share_rotation'
git status --porcelain
git fetch origin
git branch deploy/backup-before-premarket-20260817 HEAD
git switch -c deploy/premarket-20260817
git merge --no-ff --no-commit origin/agent/premarket-command
git status --short
```

先审查合并结果，再提交。若目标仓库的 `origin` 不是本交付 GitHub，新增只读 remote 后 fetch：

```powershell
git remote add premarket-source https://github.com/zhenhuajin45-lab/a-share-watchpool-daily-signal.git
git fetch premarket-source agent/premarket-command
git merge --no-ff --no-commit premarket-source/agent/premarket-command
```

合并无冲突后先运行第 10 节离线验收；全部通过再提交部署分支：

```powershell
git diff --cached --check
git commit -m "deploy: add guarded premarket command layer"
```

发生冲突时停止并保存 `git status`/冲突文件，不执行 `git checkout --theirs`、`git reset --hard` 或批量覆盖。需要逐文件确认原服务器的本地适配内容。

## 7. 离线部署方式：顺序应用补丁

1. 把 v6 ZIP 和 `.sha256` 复制到目标服务器仓库外的暂存目录。
2. 校验外层 ZIP：

```powershell
$bundle = 'D:\deploy\premarket_command_patch_bundle_v6.zip'
$expected = (Get-Content "$bundle.sha256" -Raw).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[0]
$actual = (Get-FileHash -LiteralPath $bundle -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw 'Patch bundle SHA256 mismatch' }
```

3. 解压到新的空目录并校验内部 `SHA256SUMS.txt`：

```powershell
$patchRoot = 'D:\deploy\premarket_command_patch_bundle_v6'
if (Test-Path -LiteralPath $patchRoot) { throw 'Patch staging directory already exists' }
Expand-Archive -LiteralPath $bundle -DestinationPath $patchRoot
$manifest = Get-Content -LiteralPath "$patchRoot\SHA256SUMS.txt"
foreach ($line in $manifest) {
    $parts = $line.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)
    $hash = (Get-FileHash -LiteralPath (Join-Path $patchRoot $parts[1]) -Algorithm SHA256).Hash
    if ($hash -ne $parts[0]) { throw "Patch SHA256 mismatch: $($parts[1])" }
}
```

4. 在干净部署分支按文件名顺序应用：

```powershell
Set-Location 'D:\codex\a_share_rotation'
git branch deploy/backup-before-premarket-20260817 HEAD
git switch -c deploy/premarket-20260817
$patches = Get-ChildItem -LiteralPath $patchRoot -Filter '*.patch' | Sort-Object Name
foreach ($patch in $patches) {
    git am --3way -- $patch.FullName
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Patch failed: $($patch.Name). Run git am --abort after preserving conflict evidence."
        break
    }
}
```

必须看到所有补丁 `Applying:` 成功。若失败，保存现场后运行 `git am --abort`；该命令只撤销本次未完成的补丁序列，不处理运行数据。

## 8. 作者证据迁移

`data/` 被 Git 忽略，代码部署不会自动携带 8 月 14 日作者数据。校验证据 ZIP 后，只复制原图和清单，不直接覆盖服务器已有账本：

```powershell
$evidenceZip = 'D:\deploy\author_ratio_20260814_evidence_v1.zip'
$expected = (Get-Content "$evidenceZip.sha256" -Raw).Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)[0]
$actual = (Get-FileHash -LiteralPath $evidenceZip -Algorithm SHA256).Hash
if ($actual -ne $expected) { throw 'Author evidence SHA256 mismatch' }

$stage = 'D:\deploy\author_ratio_20260814_evidence_v1'
if (Test-Path -LiteralPath $stage) { throw 'Evidence staging directory already exists' }
Expand-Archive -LiteralPath $evidenceZip -DestinationPath $stage
New-Item -ItemType Directory -Force -Path '.\data\raw\author_ratio\20260814' | Out-Null
Copy-Item -LiteralPath "$stage\20260814\article_context.png" -Destination '.\data\raw\author_ratio\20260814\article_context.png'
Copy-Item -LiteralPath "$stage\20260814\ratio_chart.png" -Destination '.\data\raw\author_ratio\20260814\ratio_chart.png'
Copy-Item -LiteralPath "$stage\20260814\article_evidence.json" -Destination '.\data\raw\author_ratio\20260814\article_evidence.json'

python .\tools\author_ratio_nightly_task.py `
  --trade-date 20260814 `
  --evidence .\data\raw\author_ratio\20260814\article_evidence.json `
  --ledger .\data\normalized\author_ratio.json
```

期望写入 `20260814=1.69`、`20260813=2.88`。已有同日不同值时脚本必须整体拒绝，不得用随包参考账本覆盖服务器真账本。

## 9. 依赖与凭据

不要在生产 Python 环境直接执行无审查的全量升级。先比较：

```powershell
python -m pip show gm numpy pandas pytest pywinauto
python -m pip check
```

本机通过版本是 Python 3.13.2、`gm 3.0.183`、`numpy 2.3.2`、`pandas 2.3.3`、`pytest 9.1.1`、`pywinauto 0.6.9`，它们是验证基线，不是强制服务器升级指令。优先保留服务器已能连接 GoldMiner 的 Python/GM 组合，只补缺失依赖并重新回归。

凭据使用交互式安全脚本注入，输入不会显示：

```powershell
. .\scripts\Set-PremarketSecrets.ps1 -PersistUserEnvironment
```

关闭并重新打开 PowerShell 后，仅检查存在性：

```powershell
if (-not $env:GM_TOKEN) { throw 'GM_TOKEN missing' }
if (-not $env:DEEPSEEK_API_KEY) { throw 'DEEPSEEK_API_KEY missing' }
if (-not $env:A_SHARE_ROTATION_FEISHU_WEBHOOK_URL) { Write-Warning 'Feishu webhook missing for existing service' }
if ($env:A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION -eq '1') { throw 'Daily-bar order execution must remain disabled' }
```

## 10. 部署后离线验收

在目标服务器部署分支执行：

```powershell
Set-Location 'D:\codex\a_share_rotation'
powershell -ExecutionPolicy Bypass -File .\scripts\validate_repository.ps1
python .\tools\validate_premarket_package.py
git diff --check
git status -sb
```

期望：

- `AST_OK files=68`
- `39 passed`
- `REPOSITORY_VALIDATION_OK`
- `no_order_api_in_command_layer=true`
- 工作区只有已知的服务器本地配置差异；未知差异视为阻断项。

## 11. GM 只读冒烟

GoldMiner 终端必须已登录，脚本与终端使用同一 Windows 用户。先把输出写入运行证据目录，不写发布目录：

```powershell
python .\adapters\gm_market_data_adapter.py `
  --trade-date 20260814 `
  --output .\reports\deployment_smoke\gm_indices_20260814.json

python .\adapters\gm_market_breadth_sector_adapter.py `
  --trade-date 20260814 `
  --output .\reports\deployment_smoke\gm_market_sector_20260814.json `
  --evidence-dir .\reports\deployment_smoke\gm_market_sector_raw `
  --include-concepts
```

通过条件：四指数 4/4、最后日期 `20260814`；全市场 bundle `status=READY`、`source_health.ready=true`、日线/行业/资金流覆盖率达到适配器门槛、errors 为空。

## 12. 2026-08-17 下一交易日运行

### 08:30 前置确认

- GoldMiner 终端已登录且数据可达。
- 当前 PowerShell 能看到轮换后的 `GM_TOKEN` 和 `DEEPSEEK_API_KEY`。
- `data/normalized/author_ratio.json` 最新核验日为 `20260814`。
- 没有同名盘前任务正在运行。
- `A_SHARE_ROTATION_ENABLE_DAILY_BAR_EXECUTION` 不是 `1`。

### 08:35-08:55 盘前主链

```powershell
Set-Location 'D:\codex\a_share_rotation'
.\scripts\Invoke-PremarketCommand.ps1 `
  -SourceTradeDate 20260814 `
  -ExecutionTradeDate 20260817 `
  -RunDeepSeek
```

在 `20+5+5` 未满足前不要传 `-PublishIfEligible`。脚本应生成：

- `data/raw/gm/20260814.json`
- `data/raw/gm_market_sector/20260814.bundle.json` 及原始 CSV.gz/SHA256
- `data/raw/external/20260817.json`
- `data/normalized/premarket_input.20260817.json`
- `reports/premarket_command/premarket_command.20260817.draft.json`
- DeepSeek prompt/raw/review/final 四份证据

预期确定性草稿在执行日外围成功后为 `READY_FOR_DEEPSEEK_REVIEW`。由于运营验收门尚未满足，DeepSeek 即使确认，最终仍应为 `REVIEW_PENDING`；这是正确保护行为。

补生成统一健康摘要：

```powershell
python .\tools\check_premarket_health.py `
  --command .\reports\premarket_command\premarket_command.20260817.draft.json `
  --gm .\data\raw\gm\20260814.json `
  --gm-market-bundle .\data\raw\gm_market_sector\20260814.bundle.json `
  --external .\data\raw\external\20260817.json `
  --author-ledger .\data\normalized\author_ratio.json `
  --output .\reports\premarket_command\health_20260817.json
```

当前 20+5+5 未满足不会使源健康变差；健康报告的 `ready` 只用于检查数据链路，正式发布仍由 review 与 release gate 共同决定。

### 09:20 影子采集

正式 `Invoke-PremarketOpeningReview.ps1` 只接受 `PUBLISHED` 基线。当前验收期不得伪造发布合同，先保存 GM 竞价快照和只收紧候选：

```powershell
python .\adapters\gm_opening_auction_adapter.py `
  --execution-date 20260817 `
  --previous-bundle .\data\raw\gm_market_sector\20260814.bundle.json `
  --output .\reports\premarket_command\gm_opening.20260817.json `
  --raw-output .\data\raw\gm_opening\20260817\ticks.csv.gz

python .\tools\build_premarket_opening_command.py `
  --published .\reports\premarket_command\premarket_command.20260817.reviewed.json `
  --gm-opening .\reports\premarket_command\gm_opening.20260817.json `
  --output .\reports\premarket_command\opening_candidate.20260817.json
```

这里 `--published` 是工具的基线参数名；在影子阶段输入 reviewed 文件只生成候选，不调用正式 opening review，也不写 published 目录。

只有在当天确实完成只读、订单不变、过期/错日期/`REVIEW_PENDING` 拒绝检查，并保留对应日志后，才可生成 shadow PASS 证据：

```powershell
python .\tools\record_premarket_acceptance.py --stage shadow --execution-date 20260817 `
  --check completed --check read_only --check orders_unchanged `
  --check stale_contract_rejected --check date_mismatch_rejected --check review_pending_rejected `
  --evidence-file .\reports\premarket_command\premarket_command.20260817.reviewed.json `
  --evidence-file .\reports\premarket_command\gm_opening.20260817.json `
  --evidence-file .\reports\premarket_command\opening_candidate.20260817.json `
  --confirm-no-real-orders `
  --output .\data\acceptance\shadow\20260817.json
```

不得为了增加计数而提前声明未验证的检查。

## 13. 飞书和现有策略预期

设置 `A_SHARE_PREMARKET_COMMAND_FILE` 可显式指定正式合同路径：

```powershell
[Environment]::SetEnvironmentVariable(
  'A_SHARE_PREMARKET_COMMAND_FILE',
  'D:\codex\a_share_rotation\data\premarket_command\published\latest.json',
  'User'
)
```

当前没有 `PUBLISHED` 合同时，飞书应出现“盘前指挥台：NOT_FOUND/NOT_PUBLISHED，不改变现有每日计划与策略门控”。这不是故障；原策略计划、信号和风控必须继续按原逻辑运行。不得让 reviewed/draft 文件冒充 `latest.json`。

## 14. Windows 任务计划关键配置

下一交易日首次部署建议人工监控运行，不建议第一天直接无人值守。稳定后再导入任务计划：

- 运行账户：GoldMiner/开盘啦当前登录用户。
- 需要 GM 终端或 UIA 的任务选择“仅当用户登录时运行”。
- 权限级别与 GoldMiner/开盘啦一致；不要无条件勾选最高权限，否则 UIA 可能因完整性级别不同而失败。
- `Start in`：`D:\codex\a_share_rotation`。
- 禁止并行新实例；失败最多重试 1 次，间隔 2 分钟。
- 08:35 主链最长运行时间建议 20 分钟；09:20 竞价任务建议 5 分钟。
- stdout/stderr 写入 `logs/premarket_command/YYYYMMDD/`，日志不得打印环境变量值。
- 周末和节假日用 GM 交易日历控制，不用星期一至星期五近似。

每次任务计划修改后导出 XML 保存到部署 Evidence Pack；导出前检查 XML 中没有 Token、API Key、Webhook 或明文密码。

## 15. 当日最终核对

不要只看进程退出码。逐项检查：

1. `health_YYYYMMDD.json` 或命令 `source_health`：`missing=[]`、`stale_or_undated=[]` 才表示数据齐全。
2. GM 原始证据日期、覆盖率和 SHA256。
3. 作者账本最新核验日期与数值。
4. DeepSeek raw 中 `credential_logged=false`；响应、解析和限权后结果均存在。
5. 最终仓位不高于确定性草稿；主攻板块没有新增。
6. `release_gate` 真实计数；未达到时 final 必须 `REVIEW_PENDING`。
7. 飞书最终事件与 outbox；不是只检查 API 请求已发出。
8. 原策略 Tick、调度、T+1 和虚拟台账状态未被覆盖。

## 16. 故障分级与处置

| 级别 | 现象 | 处置 |
| --- | --- | --- |
| P0 | 原策略无法启动、状态文件异常、出现订单执行开关为 1 | 立即停止新任务，切回备份部署分支，保留数据和日志 |
| P1 | GM 主源失败、日期错位、外围过期、DeepSeek 结果扩大权限 | 不发布；保留草稿和证据；修复后全链重跑 |
| P1 | `release_gate=MET` 但证据不足或存在 invalid 文件 | 不发布；检查 v2 schema、哈希和计数来源 |
| P2 | 开盘啦 UIA 失败 | 保存截图/文本，标记不可用；GM 草稿继续，不补造数据 |
| P2 | 公众号断更/OCR 不清 | 只记 attempt，不填 0，不沿用旧值冒充当天 |
| P2 | DeepSeek 401/超时/非 JSON | 保持 `REVIEW_PENDING`，不自动看空、不发布 |
| P3 | PowerShell 中文显示乱码 | 检查文件本身 UTF-8；不要因终端显示问题重写 JSON |

## 17. 回滚

回滚只回代码，不覆盖运行状态：

1. 停止盘前任务和相关服务，记录当前 commit 和日志位置。
2. 保留新增的 `data/raw`、账本、验收、飞书队列和 JSONL，不删除。
3. 切换到部署前保存的分支：

```powershell
git switch deploy/backup-before-premarket-20260817
```

4. 用原服务器既有启动方式启动，执行 `startup_self_check()`，核对 Tick 订阅、11 个任务、飞书启动回执和最终状态文件。
5. 若只在 `git am` 中途失败，使用 `git am --abort`；不要用 `git reset --hard`。

## 18. Go/No-Go 判定

下一交易日允许进行的动作：GM/外围只读采集、确定性草稿、DeepSeek 质检、飞书不可用状态展示、09:20 影子快照和验收记录。

下一交易日仍禁止的动作：让其它实盘策略正式消费 reviewed/draft、手工创建 `PUBLISHED`、使用指挥台新增个股、调用真实订单函数、跳过 20+5+5。

只有原服务器启动适配器、依赖、状态备份、GM/外围实测和当日最终证据均通过，才能说“下一交易日原策略稳定运行且盘前指挥台完成影子验证”；在 20+5+5 完成前不能说“盘前指挥台已经生产发布”。
