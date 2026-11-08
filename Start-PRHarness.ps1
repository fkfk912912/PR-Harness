param(
    [switch]$Check,
    [switch]$Stop,
    [switch]$Local,
    [string]$Repository
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing .venv. Create the project virtual environment and install requirements.txt first.'
}

Push-Location -LiteralPath $projectRoot
try {
    if ($Check) {
        $checkCode = @'
from pr_harness.core.config import Settings
from pr_harness.api.main import WEB_ROOT
from pathlib import Path
from scripts.run_github_webhook import resolve_github_token
settings = Settings.from_env()
settings.validate_evolution()
llm = settings.resolved_llm()
assert Path(WEB_ROOT, 'index.html').is_file()
assert Path(settings.skills_dir).is_dir()
print('Address: http://%s:%s/' % (settings.host, settings.port))
print('Provider:', llm.get('provider', 'unconfigured'))
print('Model:', llm.get('model', 'unconfigured'))
print('Model credential configured:', bool(llm.get('api_key')))
print('GitHub credential available:', bool(resolve_github_token(settings)))
print('GitHub credential source:', 'configured token' if settings.github_token else 'GitHub CLI')
print('Repair test command configured:', bool(settings.repair_test_command))
print('Authentication:', 'enabled' if settings.auth_required else 'local disabled')
print('Configuration and resource check: PASS')
'@
        & $pythonPath -B -c $checkCode
    } elseif ($Stop) {
        & $pythonPath -B .\scripts\run_github_webhook.py --stop
    } elseif ($Local) {
        Write-Host 'Starting PR Harness. Press Ctrl+C to stop.'
        & $pythonPath -B -m pr_harness
    } else {
        if (-not $Repository) {
            $statusPath = Join-Path $projectRoot 'validation\github-webhook\status.json'
            if (Test-Path -LiteralPath $statusPath) {
                $savedStatus = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
                $Repository = [string]$savedStatus.repository
            }
        }
        if (-not $Repository) {
            throw 'Specify -Repository owner/repository on the first start.'
        }
        Write-Host "Starting PR Harness with automatic GitHub review for $Repository."
        Write-Host 'Keep this window open. Press Ctrl+C to stop.'
        & $pythonPath -B .\scripts\run_github_webhook.py --repository $Repository
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PR Harness command exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
