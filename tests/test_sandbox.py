"""沙箱安全测试：合法代码放行，危险代码被拦。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app import sandbox


def _run(code: str, timeout_s: float = 10, memory_mb: int = 1024):
    import tempfile

    with tempfile.TemporaryDirectory(prefix="askdata_test_") as tmp:
        data = pd.DataFrame(
            {"订单日期": pd.to_datetime(["2025-01-05", "2025-02-12", "2025-02-20"]),
             "品类": ["电子产品", "服装", "电子产品"],
             "销售额": [100, 200, 150],
             "利润": [30, 40, 20]}
        )
        data_path = Path(tmp) / "data.csv"
        data.to_csv(data_path, index=False)
        charts_out = Path(tmp) / "charts"
        charts_out.mkdir()
        return sandbox.run_code(code, data_path, charts_out=charts_out, timeout_s=timeout_s, memory_mb=memory_mb)


def test_benign_code_returns_results_and_chart():
    code = (
        'import matplotlib\n'
        'matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans"]\n'
        'matplotlib.rcParams["axes.unicode_minus"] = False\n'
        'monthly = df.groupby(pd.to_datetime(df["订单日期"]).dt.to_period("M").astype(str))["销售额"].sum()\n'
        'save_result(monthly, "按月销售额")\n'
        'plt.figure(figsize=(8, 4))\n'
        'plt.plot(monthly.index, monthly.values, marker="o")\n'
        'plt.title("销售额趋势")\n'
        'save_chart(name="trend")\n'
    )
    outcome = _run(code)
    assert outcome.ok, outcome.message
    assert outcome.results and outcome.results[0]["kind"] == "table"
    assert outcome.charts and outcome.charts[0].file == "trend.png"
    assert outcome.code_time_ms >= 0


def test_static_scan_blocks_os_import():
    outcome = _run('import os\nprint(os.getcwd())')
    assert not outcome.ok
    assert outcome.kind == "blocked"
    assert "os" in outcome.message


def test_runtime_import_guard_blocks_smuggled_import():
    """绕过静态扫描（动态构造导入名），运行时导入守卫仍要拦下。"""
    code = 'b = globals()["__builtins__"]\nmod = b["__import__"]("o" + "s")\nprint(mod.getcwd())'
    outcome = _run(code)
    assert not outcome.ok
    assert "沙箱" in outcome.message


def test_runtime_file_guard_blocks_escape_read():
    """动态拿到 open 后读写沙箱目录之外的文件，应被拦截。"""
    code = 'b = globals()["__builtins__"]\nf = b["open"]("../../outside.txt", "w")\nf.write("x")'
    outcome = _run(code)
    assert not outcome.ok
    assert "沙箱" in outcome.message


def test_infinite_loop_hits_timeout():
    outcome = _run("while True:\n    pass\n", timeout_s=2)
    assert outcome.kind == "timeout"


def test_memory_hog_is_killed():
    outcome = _run("big = [bytearray(1024 * 1024) for _ in range(800)]\n", timeout_s=20, memory_mb=256)
    assert outcome.kind == "memory"


def test_escape_via_save_result_globals_is_blocked():
    """绕过静态扫描（字符串拼接 dunder 名），运行时 getattr 守卫仍要拦住 __globals__。"""
    code = 'g = getattr(save_result, "__" + "globals__")\nprint("LEAK", g)'
    outcome = _run(code)
    assert not outcome.ok
    assert "沙箱" in outcome.message


def test_escape_via_getattr_subclasses_is_blocked():
    """通过 getattr 拿 object.__subclasses__（经典 Python 沙箱逃逸），应被运行时守卫拦截。"""
    code = 'sc = getattr(object, "__" + "subclasses__")\nprint("SUBCLASSES", sc)'
    outcome = _run(code)
    assert not outcome.ok
    assert "沙箱" in outcome.message


def test_escape_via_numpy_ctypeslib_is_blocked():
    """合法顶层导入 numpy.ctypeslib，内部传递性 import ctypes 必须被拦下。

    两种可接受结果：导入直接报错，或导入成功但 `ncl.ctypes` 已被置空（None），
    总之不能让沙箱代码拿到可用的 ctypes 模块去做 CDLL 调用。
    """
    code = 'import numpy.ctypeslib as ncl\nprint("CTYPES_IS_NONE:", ncl.ctypes is None)'
    outcome = _run(code)
    if outcome.ok:
        assert "CTYPES_IS_NONE: True" in outcome.stdout
    else:
        assert "沙箱" in outcome.message


def test_describe_with_datetime_column_is_serializable():
    """回归：describe(include='all') 对日期列产生 Timestamp/NaN，结果必须能序列化而不是子进程崩溃。"""
    import json
    outcome = _run('save_result(df.describe(include="all"), "overview")\nsave_result(df.head(3), "head")')
    assert outcome.ok, outcome.message
    assert outcome.results
    json.dumps(outcome.results, ensure_ascii=False, allow_nan=False)


def test_plotly_pie_internal_import_allowed_but_user_importlib_blocked():
    """回归：用户代码用 plotly 画饼图时，库内部惰性 import importlib 不应被误伤；但用户主动导入 importlib 仍然拦截。"""
    code = (
        "import plotly.express as px\n"
        "g = df.groupby('\u54c1\u7c7b')['\u9500\u552e\u989d'].sum().reset_index()\n"
        "fig = px.pie(g, names='\u54c1\u7c7b', values='\u9500\u552e\u989d')\n"
        'save_chart(fig, name="share")\n'
    )
    outcome = _run(code)
    assert outcome.ok, outcome.message
    assert outcome.charts and outcome.charts[0].format == "plotly"
    blocked = _run("import importlib")
    assert not blocked.ok  # 静态扫描或运行时拦截均可
