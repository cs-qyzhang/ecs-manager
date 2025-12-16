#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install uv first: https://github.com/astral-sh/uv" >&2
  exit 1
fi

PYTHON_VERSION="${1:-3.12}"
MODE="${2:-onedir}"  # onedir | onefile

if [ ! -d ".venv" ]; then
  uv venv --python "$PYTHON_VERSION"
fi

uv sync
uv pip install -r requirements-build.txt

source .venv/bin/activate

if [ "$MODE" = "onedir" ]; then
  pyinstaller --noconfirm --clean --onedir --name ecs \
    --hidden-import shellingham.nt \
    --hidden-import shellingham.posix \
    "ecs/__main__.py"
  echo ""
  echo "Built: $(pwd)/dist/ecs/ecs"
else
  pyinstaller --noconfirm --clean --onefile --name ecs \
    --hidden-import shellingham.nt \
    --hidden-import shellingham.posix \
    "ecs/__main__.py"
  echo ""
  echo "Built: $(pwd)/dist/ecs"
fi


