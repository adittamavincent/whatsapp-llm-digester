#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR="$PROJECT_DIR/../target"

mkdir -p "$TARGET_DIR/todo" "$TARGET_DIR/markdown" "$TARGET_DIR/.cache"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "This quick setup targets Apple-silicon Macs because it uses MLX-Whisper." >&2
  echo "The converter code also supports Faster-Whisper, but that backend must be installed separately." >&2
  exit 1
fi

missing=""
for tool in ffmpeg ffprobe tesseract; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing="$missing $tool"
  fi
done

if [ -n "$missing" ]; then
  echo "Missing local tools:$missing" >&2
  echo "On macOS: brew install ffmpeg tesseract" >&2
  exit 1
fi

export UV_CACHE_DIR="$TARGET_DIR/.cache/uv"
export HF_HOME="$TARGET_DIR/.cache/huggingface"

uv sync --project "$PROJECT_DIR" --python 3.12
"$PROJECT_DIR/run.sh" doctor

echo
echo "Ready. Drop inputs in: $TARGET_DIR/todo"
echo "Run: $PROJECT_DIR/run.sh"
