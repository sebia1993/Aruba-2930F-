[CmdletBinding()]
param(
    [string]$PythonPath = "python",
    [switch]$SkipAudit,
    [switch]$SkipCoverage
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Push-Location $repoRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    $env:QT_QPA_PLATFORM = "offscreen"

    Invoke-Checked $PythonPath @("-c", "import platform,sys; assert sys.version_info[:2] == (3,14); assert platform.architecture()[0] == '64bit'")
    Invoke-Checked $PythonPath @("-m", "pip", "check")
    Invoke-Checked $PythonPath @("tools/check_version.py")
    Invoke-Checked $PythonPath @("tools/check_no_secrets.py")
    Invoke-Checked $PythonPath @("-m", "ruff", "check", ".")
    Invoke-Checked $PythonPath @("-m", "ruff", "format", "--check", ".")
    Invoke-Checked $PythonPath @("-m", "mypy", "src")

    $pytestArguments = @("-m", "pytest")
    if (-not $SkipCoverage) {
        $coveragePath = Join-Path $repoRoot "artifacts\coverage.xml"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $coveragePath) | Out-Null
        $pytestArguments += @(
            "--cov=aruba2930f_backup",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=xml:$coveragePath"
        )
    }
    Invoke-Checked $PythonPath $pytestArguments

    if (-not $SkipAudit) {
        # Paramiko 4 is required only for explicitly documented legacy 2930F SSH support.
        Invoke-Checked $PythonPath @(
            "-m", "pip_audit", "-r", "requirements-lock.txt", "--strict",
            "--ignore-vuln", "PYSEC-2026-2858"
        )
    }
    Write-Host "Repository validation passed."
}
finally {
    Pop-Location
}
