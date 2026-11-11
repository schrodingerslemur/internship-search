# Upload data/apply/profile.json and its resume to the hosted database, so the
# cloud auto-apply worker can apply as you. Run again after editing either.
#
# Loads .env.apply (git-ignored) like scripts/apply.ps1, so it writes to the
# same Neon database the dashboard and the worker use.
#
#   .\scripts\push-profile.ps1
#   .\scripts\push-profile.ps1 --email you@example.com   (shared database)

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
    Write-Host "No .env.apply found; this writes to the database in .env, not the hosted one." -ForegroundColor Yellow
}

& (Join-Path $root ".venv\Scripts\python.exe") -m app.cli autoapply-profile @args
exit $LASTEXITCODE
