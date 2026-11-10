# Open today's applications, filled in. You review each one and click Submit.
#
# Loads .env.apply (git-ignored) on top of .env, so the assistant reads the
# same database as the daily digest -- normally the Neon DATABASE_URL -- while
# local development keeps its SQLite file. Arguments pass straight through:
#
#   .\scripts\apply.ps1                 prepare and open
#   .\scripts\apply.ps1 --prepare-only
#   .\scripts\apply.ps1 --job <greenhouse link>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$envFile = Join-Path $root ".env.apply"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $name, $value = $trimmed.Split("=", 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim().Trim('"'), "Process")
    }
} else {
    Write-Host "No .env.apply found; using the database in .env." -ForegroundColor Yellow
}

& (Join-Path $root ".venv\Scripts\python.exe") -m app.cli apply @args
exit $LASTEXITCODE
