#!/usr/bin/env bash
# AskData 启动脚本（Linux / macOS / Hugging Face Spaces / Render）
set -e
export ASKDATA_LLM_PROVIDER="${ASKDATA_LLM_PROVIDER:-mock}"
export ASKDATA_DATA_DIR="${ASKDATA_DATA_DIR:-$PWD/data}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

