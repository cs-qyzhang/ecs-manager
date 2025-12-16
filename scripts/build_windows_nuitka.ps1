param(
  [string]$Python = "3.12",
  [switch]$SyncDeps,
  [switch]$Clean
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv not found. Install uv first: https://github.com/astral-sh/uv"
}

if (-not (Test-Path ".venv")) {
  uv venv --python $Python
  $SyncDeps = $true
}

if ($SyncDeps) {
  uv sync
  uv pip install -r requirements-build.txt
}

# Nuitka builds native code. On Windows you need a C compiler toolchain.
# Easiest: Visual Studio Build Tools, or use MinGW64 via --mingw64 (Nuitka may download).

$pythonExe = Join-Path $repo ".venv\\Scripts\\python.exe"
if (-not (Test-Path $pythonExe)) {
  throw "python.exe not found at $pythonExe"
}

# Optional clean build (forces recompilation)
if ($Clean) {
  if (Test-Path "dist_nuitka\\__main__.build") { Remove-Item -Recurse -Force "dist_nuitka\\__main__.build" }
  if (Test-Path "dist_nuitka\\__main__.dist") { Remove-Item -Recurse -Force "dist_nuitka\\__main__.dist" }
  if (Test-Path "dist_nuitka\\__main__.onefile-build") { Remove-Item -Recurse -Force "dist_nuitka\\__main__.onefile-build" }
}

# Include aliyunsdkcore/data/*.json (Nuitka does not reliably include package data automatically)
$aliyunDataDir = & $pythonExe -c "import pathlib,aliyunsdkcore; print(pathlib.Path(aliyunsdkcore.__file__).parent/'data')"
$aliyunDataDir = $aliyunDataDir.Trim()
if (-not (Test-Path $aliyunDataDir)) {
  throw "aliyunsdkcore data dir not found: $aliyunDataDir"
}

# Include Aliyun SDK vendored CA bundle (requests/certifi) for TLS verification
$aliyunCaBundle = & $pythonExe -c "import pathlib,aliyunsdkcore; print(pathlib.Path(aliyunsdkcore.__file__).parent/'vendored'/'requests'/'packages'/'certifi'/'cacert.pem')"
$aliyunCaBundle = $aliyunCaBundle.Trim()
if (-not (Test-Path $aliyunCaBundle)) {
  throw "aliyunsdkcore CA bundle not found: $aliyunCaBundle"
}

& $pythonExe -m nuitka `
  --standalone `
  --onefile `
  --assume-yes-for-downloads `
  --output-dir=dist_nuitka `
  --output-filename=ecs.exe `
  --include-data-dir="$aliyunDataDir=aliyunsdkcore/data" `
  --include-data-file="$aliyunCaBundle=aliyunsdkcore/vendored/requests/packages/certifi/cacert.pem" `
  "ecs\\__main__.py"

Write-Host ""
Write-Host "Built: $(Join-Path $repo 'dist_nuitka\\ecs.exe')"


