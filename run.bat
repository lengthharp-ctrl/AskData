@echo off
REM AskData 本地启动脚本（Windows）
REM 首次使用请先安装依赖：python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
setlocal
if not defined ASKDATA_LLM_PROVIDER set ASKDATA_LLM_PROVIDER=mock
if not defined ASKDATA_DATA_DIR set ASKDATA_DATA_DIR=D:\Codex\workspace\AskData\data
if not exist ".venv\Scripts\python.exe" (
  echo [AskData] 未找到 .venv，正在创建虚拟环境并安装依赖...
  python -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || exit /b 1
)
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

