"""AskData —— 自然语言数据分析助手。"""

import os as _os

# 限缩数值计算库的线程数：容器（如 Render 免费实例）能看到宿主机的全部
# CPU 核，OpenBLAS/OpenMP 会按核数为每个线程预分配工作区，在 512MB 小内存
# 实例上会直接报「OpenBLAS error: Memory allocation failed」。
# 必须在任何 import numpy/pandas 之前设置。
for _var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    _os.environ.setdefault(_var, "1")

del _os, _var
