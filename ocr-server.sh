#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OCR_PYTHON="$PROJECT_DIR/ocr/.venv/bin/python"
if [ ! -x "$PROJECT_DIR/ocr/.venv/bin/mlx_vlm.server" ]; then
  echo "Install the local Metal runtime first: $PROJECT_DIR/setup-ocr.sh --extra metal" >&2
  exit 1
fi

export HF_HOME="$PROJECT_DIR/.cache/paddleocr/huggingface"
exec "$OCR_PYTHON" -m mlx_vlm.server \
  --model PaddlePaddle/PaddleOCR-VL-1.6 \
  --host 127.0.0.1 --port 8111 --max-num-seqs 1 --log-progress-interval 0
