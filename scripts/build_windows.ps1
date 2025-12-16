param(
  [string]$Python = "3.12",
  [ValidateSet("onefile","onedir")][string]$Mode = "onedir"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv not found. Install uv first: https://github.com/astral-sh/uv"
}

if (-not (Test-Path ".venv")) {
  uv venv --python $Python
}

uv sync
uv pip install -r requirements-build.txt

$pyinstaller = Join-Path $repo ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyinstaller)) {
  throw "pyinstaller.exe not found at $pyinstaller"
}

if ($Mode -eq "onedir") {
  & $pyinstaller --noconfirm --clean --onedir --name ecs `
    --hidden-import shellingham.nt `
    --hidden-import shellingham.posix `
    "ecs\__main__.py"
  Write-Host ""
  Write-Host "Built: $(Join-Path $repo 'dist\ecs\ecs.exe')"
} else {
  & $pyinstaller --noconfirm --clean --onefile --name ecs `
    --hidden-import shellingham.nt `
    --hidden-import shellingham.posix `
    "ecs\__main__.py"
  Write-Host ""
  Write-Host "Built: $(Join-Path $repo 'dist\ecs.exe')"
}


