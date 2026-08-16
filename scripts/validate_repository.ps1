$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"

python -c "import ast,pathlib; files=list(pathlib.Path('src').rglob('*.py'))+list(pathlib.Path('adapters').rglob('*.py'))+list(pathlib.Path('tools').rglob('*.py')); [ast.parse(p.read_text(encoding='utf-8'), filename=str(p)) for p in files]; print(f'AST_OK files={len(files)}')"
if ($LASTEXITCODE -ne 0) { throw "AST validation failed" }
python -m json.tool .\universe\sector_taxonomy.json > $null
if ($LASTEXITCODE -ne 0) { throw "sector taxonomy JSON validation failed" }
python -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
python .\tools\validate_premarket_package.py
if ($LASTEXITCODE -ne 0) { throw "premarket package validation failed" }
python -c "import sys; sys.path.insert(0, 'src'); import action_layer, dynamic_universe, intraday_engine, live_signal_service, market_permission, market_sector_feed, signal_rules, premarket_command.engine, premarket_command.opening_review, premarket_command.publisher; print('IMPORT_SMOKE_OK modules=10')"
if ($LASTEXITCODE -ne 0) { throw "import smoke test failed" }
git diff --check
if ($LASTEXITCODE -ne 0) { throw "git diff check failed" }

Write-Host "REPOSITORY_VALIDATION_OK"
