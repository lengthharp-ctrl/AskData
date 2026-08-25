"""AskData 全局配置：全部通过环境变量注入，便于本地 / Hugging Face / Render 切换。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ----------------------------- LLM -----------------------------
# 可选值：mock（无 Key 演示/测试）| deepseek | gemini | openai（任意兼容接口）
LLM_PROVIDER = os.getenv("ASKDATA_LLM_PROVIDER", "mock").lower()
LLM_API_KEY = os.getenv("ASKDATA_LLM_API_KEY", "").strip()

# DeepSeek / Gemini 都走 OpenAI 兼容接口，切换只改一个环境变量
LLM_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
}
LLM_BASE_URL = os.getenv("ASKDATA_LLM_BASE_URL", "").strip()
LLM_MODEL = os.getenv("ASKDATA_LLM_MODEL", "").strip()
LLM_TIMEOUT_S = float(os.getenv("ASKDATA_LLM_TIMEOUT_S", "60"))
LLM_TEMPERATURE = float(os.getenv("ASKDATA_LLM_TEMPERATURE", "0.1"))


def llm_settings() -> tuple[str, str]:
    """返回 (base_url, model)，未显式指定时按 provider 预设回退。"""
    preset = LLM_PRESETS.get(LLM_PROVIDER, {})
    base_url = LLM_BASE_URL or preset.get("base_url", "")
    model = LLM_MODEL or preset.get("model", "")
    return base_url, model


# ----------------------------- 沙箱 -----------------------------
SANDBOX_TIMEOUT_S = float(os.getenv("ASKDATA_SANDBOX_TIMEOUT_S", "5"))
SANDBOX_MEMORY_MB = int(os.getenv("ASKDATA_SANDBOX_MEMORY_MB", "512"))
SANDBOX_STARTUP_TIMEOUT_S = float(os.getenv("ASKDATA_SANDBOX_STARTUP_TIMEOUT_S", "90"))

# ----------------------------- 数据 -----------------------------
MAX_UPLOAD_MB = int(os.getenv("ASKDATA_MAX_UPLOAD_MB", "20"))
MAX_ROWS = int(os.getenv("ASKDATA_MAX_ROWS", "200000"))
MAX_COLS = int(os.getenv("ASKDATA_MAX_COLS", "200"))

DATA_DIR = Path(os.getenv("ASKDATA_DATA_DIR", str(BASE_DIR / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
MEDIA_DIR = DATA_DIR / "media"
SESSION_TTL_MINUTES = int(os.getenv("ASKDATA_SESSION_TTL_MINUTES", "120"))

SAMPLE_CSV = DATA_DIR / "sample_sales.csv"


def ensure_dirs() -> None:
    """确保数据目录存在（上传、图表媒体、示例数据）。"""
    for d in (UPLOAD_DIR, MEDIA_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)

