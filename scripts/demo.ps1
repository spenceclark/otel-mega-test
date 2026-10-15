# Windows wrapper: .\scripts\demo.ps1 <verb> [options]   (see scripts/demo.py)
$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    uv run --quiet scripts/demo.py @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
