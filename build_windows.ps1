[CmdletBinding()]
param(
    [string]$PythonPath = "python",

    [ValidatePattern("^\d+\.\d+\.\d+$")]
    [string]$Version = "0.1.1",

    [switch]$SkipValidation,
    [switch]$UseCurrentEnvironment,
    [switch]$AllowDirty
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$artifactsRoot = Join-Path $repoRoot "artifacts"
$buildRoot = Join-Path $artifactsRoot "build"
$distRoot = Join-Path $artifactsRoot "dist"
$releaseRoot = Join-Path $artifactsRoot "release"
$productName = "Aruba2930FConfigBackup"
$bundleRoot = Join-Path $distRoot $productName

function Resolve-PhysicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    $current = $root
    foreach ($segment in ($fullPath.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })) {
        $candidate = Join-Path $current $segment
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -Force -LiteralPath $candidate
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $target = [string]@($item.Target)[0]
                if ([string]::IsNullOrEmpty($target)) {
                    throw "Could not resolve reparse point target: $candidate"
                }
                if (-not [IO.Path]::IsPathRooted($target)) {
                    $target = Join-Path (Split-Path -Parent $candidate) $target
                }
                $current = [IO.Path]::GetFullPath($target)
                continue
            }
        }
        $current = $candidate
    }
    return [IO.Path]::GetFullPath($current)
}

function Assert-RepoChild {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $repoPrefix = $repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing filesystem mutation outside the repository: $fullPath"
    }
    if ($fullPath -eq $repoRoot) {
        throw "Refusing filesystem mutation against the repository root."
    }
    $current = $fullPath
    while (-not $current.Equals($repoRoot, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -Force -LiteralPath $current
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing filesystem mutation through a reparse point: $current"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $current) {
            throw "Could not prove filesystem mutation stays inside the repository: $fullPath"
        }
        $current = $parent
    }
    return $fullPath
}

function Remove-SafeDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = Assert-RepoChild $Path
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

function Get-GitStatusSnapshot {
    $lines = @(& git status --porcelain=v1 --untracked-files=all 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the complete Git working tree state."
    }
    return [string]::Join("`n", $lines)
}

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

Push-Location $repoRoot
try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:QT_QPA_PLATFORM = "offscreen"

    Invoke-Checked $PythonPath @(
        "-c",
        "import platform,sys; assert sys.version_info[:2] == (3,14), sys.version; assert platform.system() == 'Windows'; assert platform.machine().upper() in {'AMD64','X86_64'}"
    )
    Invoke-Checked $PythonPath @("tools/check_version.py", "--expected", $Version)

    $gitCommit = "unknown"
    $gitDirty = $true
    $gitStateBefore = ""
    $gitStateAvailable = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitTopLevelOutput = & git rev-parse --show-toplevel 2>$null
        $gitTopLevel = ([string]$gitTopLevelOutput).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($gitTopLevel)) {
            throw "Could not verify the Git repository root."
        }
        $resolvedGitRoot = Resolve-PhysicalPath $gitTopLevel
        $resolvedRepoRoot = Resolve-PhysicalPath $repoRoot
        if (-not $resolvedGitRoot.Equals($resolvedRepoRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Git root does not match the build repository: $resolvedGitRoot"
        }
        $gitCommitOutput = & git rev-parse --verify HEAD 2>$null
        $gitCommit = ([string]$gitCommitOutput).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($gitCommit)) {
            throw "Could not resolve the build commit."
        }
        $gitStateBefore = Get-GitStatusSnapshot
        $gitStateAvailable = $true
        $gitDirty = -not [string]::IsNullOrEmpty($gitStateBefore)
    }
    if ($gitDirty -and -not $AllowDirty) {
        throw "Refusing a release build from a dirty or non-Git source tree. Commit the intended files or pass -AllowDirty for a non-release test build."
    }

    Remove-SafeDirectory $buildRoot
    Remove-SafeDirectory $distRoot
    Remove-SafeDirectory $releaseRoot
    New-Item -ItemType Directory -Force -Path $buildRoot, $distRoot, $releaseRoot | Out-Null

    $toolPython = $PythonPath
    if (-not $UseCurrentEnvironment) {
        $toolVenvRoot = Join-Path $buildRoot "tooling-venv"
        Invoke-Checked $PythonPath @("-m", "venv", $toolVenvRoot)
        $toolPython = Join-Path $toolVenvRoot "Scripts\python.exe"
        Invoke-Checked $toolPython @(
            "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "pip==26.2.1", "setuptools==84.0.0"
        )
        Invoke-Checked $toolPython @(
            "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "-r", "requirements-lock.txt", "-r", "requirements-dev.txt"
        )
        Invoke-Checked $toolPython @(
            "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--no-deps", "-e", "."
        )
    }

    if (-not $SkipValidation) {
        & (Join-Path $repoRoot "tools\validate.ps1") -PythonPath $toolPython
    }

    # Build from a runtime-only environment so optional development packages do
    # not leak into the executable or make the published SBOM inaccurate.
    $packageVenvRoot = Join-Path $buildRoot "package-venv"
    Invoke-Checked $PythonPath @("-m", "venv", $packageVenvRoot)
    $packagePython = Join-Path $packageVenvRoot "Scripts\python.exe"
    Invoke-Checked $packagePython @(
        "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "pip==26.2.1", "setuptools==84.0.0"
    )
    Invoke-Checked $packagePython @(
        "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "-r", "requirements-lock.txt", "pyinstaller==6.22.2"
    )
    Invoke-Checked $packagePython @(
        "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--no-deps", "-e", "."
    )

    Invoke-Checked $packagePython @(
        "-m", "PyInstaller", "--clean", "--noconfirm",
        "--workpath", (Join-Path $buildRoot "pyinstaller"),
        "--distpath", $distRoot,
        (Join-Path $repoRoot "packaging\Aruba2930FConfigBackup.spec")
    )

    if ($gitStateAvailable -and (Get-GitStatusSnapshot) -ne $gitStateBefore) {
        throw "The source tree changed while the release package was being built."
    }

    $exePath = Join-Path $bundleRoot "$productName.exe"
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "PyInstaller did not produce the expected EXE: $exePath"
    }

    foreach ($document in @("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md")) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $document) -Destination $bundleRoot
    }

    $buildInfo = [ordered]@{
        product = $productName
        version = $Version
        architecture = "windows-x64"
        commit = $gitCommit
        dirtyTree = $gitDirty
        python = (& $packagePython -c "import platform; print(platform.python_version())").Trim()
        packagedAtUtc = [DateTime]::UtcNow.ToString("o")
        authenticodeSigned = $false
        liveDeviceValidated = $false
    } | ConvertTo-Json
    [IO.File]::WriteAllText(
        (Join-Path $bundleRoot "BUILD_INFO.json"),
        $buildInfo + "`n",
        [Text.UTF8Encoding]::new($false)
    )

    $sbomName = "$productName`_v$Version`_sbom.cdx.json"
    $sbomPath = Join-Path $releaseRoot $sbomName
    Invoke-Checked $toolPython @(
        "-m", "cyclonedx_py", "requirements", "requirements-lock.txt",
        "--pyproject", "pyproject.toml",
        "--mc-type", "application",
        "--output-reproducible",
        "--sv", "1.6",
        "--of", "JSON",
        "-o", $sbomPath
    )
    Copy-Item -LiteralPath $sbomPath -Destination (Join-Path $bundleRoot "sbom.cdx.json")

    $zipName = "$productName`_v$Version`_windows_x64.zip"
    $zipPath = Join-Path $releaseRoot $zipName
    Compress-Archive -LiteralPath $bundleRoot -DestinationPath $zipPath -CompressionLevel Optimal

    $digest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $sidecarPath = "$zipPath.sha256"
    [IO.File]::WriteAllText(
        $sidecarPath,
        "$digest  $zipName`n",
        [Text.UTF8Encoding]::new($false)
    )

    $verifyArguments = @(
        "tools/verify_release.py",
        "--zip", $zipPath,
        "--sha256", $sidecarPath,
        "--sbom", $sbomPath,
        "--version", $Version,
        "--smoke"
    )
    if ($AllowDirty) {
        $verifyArguments += "--allow-dirty"
    }
    Invoke-Checked $toolPython $verifyArguments
    Invoke-Checked $toolPython @(
        "tools/release_notes.py", "--version", $Version,
        "--output", (Join-Path $releaseRoot "release-notes-v$Version.md")
    )
    if ($gitStateAvailable -and (Get-GitStatusSnapshot) -ne $gitStateBefore) {
        throw "The source tree changed before release asset completion."
    }

    Write-Host "Windows portable release assets created in $releaseRoot"
    Get-ChildItem -LiteralPath $releaseRoot | Select-Object Name, Length
}
finally {
    Pop-Location
}
