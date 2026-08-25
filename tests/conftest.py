"""测试环境：强制 mock LLM、独立临时数据目录。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_TEST_DATA = Path(tempfile.mkdtemp(prefix="askdata_test_data_"))
os.environ["ASKDATA_LLM_PROVIDER"] = "mock"
os.environ["ASKDATA_DATA_DIR"] = str(_TEST_DATA)
os.environ["ASKDATA_SANDBOX_TIMEOUT_S"] = "10"
os.environ["ASKDATA_SANDBOX_MEMORY_MB"] = "1024"

