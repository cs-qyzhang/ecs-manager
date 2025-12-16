#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install uv first: https://github.com/astral-sh/uv" >&2
  exit 1
fi

PYTHON_VERSION="${1:-3.12}"

if [ ! -d ".venv" ]; then
  uv venv --python "$PYTHON_VERSION"
fi

uv sync
uv pip install -r requirements-build.txt

source .venv/bin/activate

# Include aliyunsdkcore/data/*.json (Nuitka does not reliably include package data automatically)
ALIYUN_DATA_DIR="$(python -c 'import pathlib,aliyunsdkcore; print(pathlib.Path(aliyunsdkcore.__file__).parent / "data")')"
ALIYUN_CA_BUNDLE="$(python -c 'import pathlib,aliyunsdkcore; print(pathlib.Path(aliyunsdkcore.__file__).parent / "vendored" / "requests" / "packages" / "certifi" / "cacert.pem")')"

# Nuitka builds native code. You need Xcode Command Line Tools on macOS:
#   xcode-select --install
python -m nuitka \
  --standalone \
  --onefile \
  --assume-yes-for-downloads \
  --output-dir=dist_nuitka \
  --output-filename=ecs \
  --include-data-dir="$ALIYUN_DATA_DIR=aliyunsdkcore/data" \
  --include-data-file="$ALIYUN_CA_BUNDLE=aliyunsdkcore/vendored/requests/packages/certifi/cacert.pem" \
  "ecs/__main__.py"

echo ""
echo "Built: $(pwd)/dist_nuitka/ecs"


