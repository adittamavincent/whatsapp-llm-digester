#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export UV_CACHE_DIR="$PROJECT_DIR/.cache/uv-ocr"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv sync --project "$PROJECT_DIR/ocr" --python 3.12 "$@"
echo "OCR environment ready. Run: $PROJECT_DIR/run.sh --ocr-engine paddleocr"
echo "The first conversion downloads PaddleOCR-VL-1.6 and layout/preprocessing models."
