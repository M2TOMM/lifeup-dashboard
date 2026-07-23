param(
    [string]$Version = "1.2.0"
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$WorkRoot = Join-Path $Repo "work\release-build"
$Venv = Join-Path $Repo "work\release-venv"
$Stage = Join-Path $WorkRoot "LifeUpDashboard-$Version-windows-x64"
$Dist = Join-Path $WorkRoot "dist"
$Output = Join-Path $Repo "outputs\LifeUpDashboard-$Version-windows-x64.zip"

if (-not $WorkRoot.StartsWith((Join-Path $Repo "work"), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean a build path outside the project work directory."
}
if (Test-Path -LiteralPath $WorkRoot) {
    Remove-Item -LiteralPath $WorkRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Venv "Scripts\python.exe"))) {
    py -3.11 -m venv $Venv
}
$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --disable-pip-version-check -r (Join-Path $Repo "requirements-desktop.txt")

& $Python -m unittest discover -s (Join-Path $Repo "tests")
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "LifeUpDashboard" `
    --distpath $Dist `
    --workpath (Join-Path $WorkRoot "pyinstaller") `
    --specpath (Join-Path $WorkRoot "spec") `
    --collect-all webview `
    --add-data "$(Join-Path $Repo 'index.html');." `
    (Join-Path $Repo "desktop_app.py")

$Exe = Join-Path $Dist "LifeUpDashboard.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "PyInstaller did not create LifeUpDashboard.exe"
}
Copy-Item -LiteralPath $Exe -Destination (Join-Path $Stage "LifeUpDashboard.exe")
Copy-Item -LiteralPath (Join-Path $Repo "docs\USER_GUIDE.md") -Destination (Join-Path $Stage "USER_GUIDE.md")
@"
LifeUp Dashboard $Version
Build date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')
User data directory: %LOCALAPPDATA%\LifeUpDashboard
The package contains no LifeUp backups, tokens, local configuration, workspaces, logs, or database copies.
"@ | Set-Content -LiteralPath (Join-Path $Stage "RELEASE.txt") -Encoding UTF8

& $Python (Join-Path $Repo "tools\audit_release.py") $Exe $Stage
if (Test-Path -LiteralPath $Output) {
    Remove-Item -LiteralPath $Output -Force
}
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Output -CompressionLevel Optimal
& $Python (Join-Path $Repo "tools\audit_release.py") $Output

$Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Output
Write-Output "Release: $Output"
Write-Output "SHA256: $($Hash.Hash)"
