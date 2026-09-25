#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR="$PROJECT_DIR/../target"

mkdir -p "$TARGET_DIR/todo" "$TARGET_DIR/markdown" "$TARGET_DIR/.cache"

export UV_CACHE_DIR="$TARGET_DIR/.cache/uv"
export HF_HOME="$TARGET_DIR/.cache/huggingface"

exec uv run --project "$PROJECT_DIR" python "$PROJECT_DIR/digest.py" "$@"
