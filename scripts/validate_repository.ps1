$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"

python -c "import ast,pathlib; files=list(pathlib.Path('src').glob('*.py')); [ast.parse(p.read_text(encoding='utf-8'), filename=str(p)) for p in files]; print(f'AST_OK files={len(files)}')"
python -m json.tool .\universe\sector_taxonomy.json > $null
python -m pytest -q
python -c "import sys; sys.path.insert(0, 'src'); import action_layer, dynamic_universe, intraday_engine, live_signal_service, market_permission, market_sector_feed, signal_rules; print('IMPORT_SMOKE_OK modules=7')"
git diff --check

Write-Host "REPOSITORY_VALIDATION_OK"
