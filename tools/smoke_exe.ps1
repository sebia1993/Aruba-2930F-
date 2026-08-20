[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,

    [ValidateRange(5, 120)]
    [int]$TimeoutSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$resolvedExe = (Resolve-Path -LiteralPath $ExePath).Path
if ([IO.Path]::GetExtension($resolvedExe) -ne ".exe") {
    throw "Smoke target is not an EXE: $resolvedExe"
}

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $resolvedExe
$startInfo.Arguments = "--smoke-test"
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.EnvironmentVariables["QT_QPA_PLATFORM"] = "offscreen"
$startInfo.EnvironmentVariables["PYTHONUTF8"] = "1"

$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
if (-not $process.Start()) {
    throw "Packaged application did not start."
}

try {
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill()
        throw "Packaged smoke test exceeded $TimeoutSeconds seconds."
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged smoke test failed with exit code $($process.ExitCode)."
    }
}
finally {
    $process.Dispose()
}

Write-Host "Packaged EXE smoke test passed: $resolvedExe"
