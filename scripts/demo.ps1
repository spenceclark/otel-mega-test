# Windows wrapper: .\scripts\demo.ps1 <verb> [options]   (see scripts/demo.py)
# No $ErrorActionPreference='Stop': Windows PowerShell 5.1 turns docker's stderr progress into terminating
# errors when output is redirected. Success/failure comes from the exit code only.
Push-Location (Join-Path $PSScriptRoot '..')
try {
    uv run --quiet scripts/demo.py @args
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $code
