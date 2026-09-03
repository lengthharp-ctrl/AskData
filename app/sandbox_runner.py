"""AskData 沙箱执行器（子进程侧）。

由 app/sandbox.py 以独立 subprocess 启动，职责：
1. 预加载白名单库（pandas/numpy/matplotlib/plotly），避免运行期内部导入被护栏误伤；
2. 安装运行时护栏：白名单导入、禁用网络、文件读写限定沙箱目录、禁用危险内建与 os 进程调用；
3. 以受限 globals 执行 LLM 生成的代码；
4. 把结构化结果（表格/图表/文本）写回 JSON 结果文件，供父进程读取。

设计说明：进程内 Python 沙箱无法做到绝对安全（这是语言特性），
因此本项目采用「静态扫描 + 运行时护栏 + 超时 + 内存限制 + 独立进程」纵深防御，
生产环境建议再叠加容器隔离（见 README「沙箱设计」）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
import types

# 小内存容器（Render 免费实例 512MB）必须把数值库线程压到 1，否则 OpenBLAS
# 按宿主机核数预分配工作区，子进程会 Memory allocation failed 直接退出。
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

SANDBOX_DIR = os.environ.get("ASKDATA_SANDBOX_DIR") or os.getcwd()
CHART_DIR = os.path.join(SANDBOX_DIR, "charts")
os.makedirs(CHART_DIR, exist_ok=True)

# ---------------------------------------------------------------
# 阶段 1：预加载（此时护栏未安装，允许访问系统资源）
# ---------------------------------------------------------------
import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# 预热字体缓存（把系统字体索引写进沙箱目录），渲染中文字体不再需要读系统目录
try:
    import matplotlib.font_manager as _font_manager

    _font_manager.findfont(_font_manager.FontProperties(family="sans-serif"))
except Exception:
    pass

# 顶层预加载 plotly：把较重的 import 放在「启动到 READY」阶段（有 90s 启动额度），
# 避免挤占「代码执行」时限（默认仅 5s，小 CPU 实例上首次 import plotly 就可能超时）。
# 内存峰值实测约 205MB，512MB 实例余量充足。
try:
    import plotly.express as px  # noqa: E402
    import plotly.graph_objects as go  # noqa: E402
except Exception:
    px = go = None

try:
    from PIL import Image  # noqa: E402  (matplotlib 保存 PNG 时惰性依赖)
except Exception:
    pass

print("READY", file=sys.stderr, flush=True)

# ---------------------------------------------------------------
# 阶段 2：运行时护栏
# ---------------------------------------------------------------

# 第三方白名单（数据分析所需的库，全部无害）
_THIRD_PARTY_ALLOWED = {
    "pandas", "numpy", "matplotlib", "plotly",
    "openpyxl", "dateutil", "pytz", "packaging", "PIL",
}

# 标准库默认放行，但剔除一切可能造成伤害的模块（网络/进程/文件系统逃逸/解压炸弹等）
_BLOCKED_STDLIB = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "pickle",
    "marshal", "ctypes", "importlib", "builtins", "multiprocessing",
    "urllib", "http", "ftplib", "smtplib", "ssl", "webbrowser", "pty",
    "resource", "signal", "platform", "pdb", "code", "codeop",
    "compileall", "dis", "runpy", "zipfile", "tarfile", "gzip", "bz2",
    "lzma", "zlib", "sqlite3", "dbm", "shelve", "tkinter", "turtle",
    "curses", "getpass", "posix", "pwd", "grp", "termios", "fcntl",
    "mmap", "email", "xml", "xmlrpc", "asyncio", "concurrent", "socket",
}

ALLOWED_TOP_LEVEL = _THIRD_PARTY_ALLOWED | (
    set(sys.stdlib_module_names) - _BLOCKED_STDLIB
)


def _deny(message: str):
    def _blocked(*args, **kwargs):
        raise RuntimeError(message)

    return _blocked


_NET_DENY = _deny("沙箱：网络访问已禁用")

# 2.1 禁用网络：即使拿到 socket 模块对象，所有关键入口都会抛错
import socket as _socket_mod  # noqa: E402

for _name in (
    "socket", "create_connection", "create_server", "socketpair", "fromfd",
    "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
    "sendall", "send", "connect", "connect_ex", "bind", "listen", "accept",
    "recv", "recvfrom", "sendto", "send_fds", "recv_fds",
):
    if hasattr(_socket_mod, _name):
        setattr(_socket_mod, _name, _NET_DENY)

# 2.2 禁用 os 的进程执行能力（pandas/numpy 内部引用的 os 是同一个模块对象，一并生效）
import os as _os_mod  # noqa: E402

for _name in (
    "system", "popen", "startfile", "fork", "posix_spawn", "posix_spawnp",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
    "spawnvp", "spawnvpe", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe",
):
    if hasattr(_os_mod, _name):
        setattr(_os_mod, _name, _deny(f"沙箱：禁止调用 os.{_name}"))

# 2.3 文件读写限制：只允许沙箱目录；系统字体与已安装库的数据文件只读放行
_real_open = open

_READ_ALLOW_ROOTS = []
for _p in (
    r"C:\Windows\Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    matplotlib.get_data_path(),
):
    _r = os.path.realpath(str(_p))
    if os.path.isdir(_r):
        _READ_ALLOW_ROOTS.append(_r)

# 白名单库（plotly/pandas/matplotlib 等）运行期会读取自身数据文件，只读放行 site-packages
for _sp in (
    os.path.join(sys.prefix, "Lib", "site-packages"),
    os.path.join(sys.prefix, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"),
    os.path.join(sys.prefix, "local", "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"),
):
    _r = os.path.realpath(_sp)
    if os.path.isdir(_r):
        _READ_ALLOW_ROOTS.append(_r)


def _guarded_open(file, mode="r", *args, **kwargs):
    if isinstance(file, (str, os.PathLike)):
        raw = os.fspath(file)
        p = os.path.realpath(raw)
        root = os.path.realpath(SANDBOX_DIR)
        inside = p == root or p.startswith(root + os.sep)
        if not inside:
            if "r" in mode and any(p.startswith(r + os.sep) for r in _READ_ALLOW_ROOTS):
                return _real_open(file, mode, *args, **kwargs)
            raise PermissionError(f"沙箱：文件访问被限制在临时目录内：{raw!r}")
    return _real_open(file, mode, *args, **kwargs)


_real_import = __import__


# 对所有调用方（包括库内部惰性 import）一律禁止的危险顶层模块。
# 这些模块要么能直接执行任意代码（ctypes/subprocess/importlib/runpy/multiprocessing），
# 要么能反序列化出代码执行（pickle/marshal）。数据分析运行期用不到它们。
_DANGEROUS_ALWAYS_BLOCKED = {
    "ctypes", "subprocess", "importlib", "multiprocessing", "pickle", "marshal",
    "runpy", "code", "codeop", "pty", "posix",
}
# 分级：下列模块是白名单库（plotly/pandas）惰性加载时会内部用到的「机制类」模块，
# 只拦用户代码的主动导入，库内部调用放行；其余硬危险模块（ctypes 等逃逸终点）不分调用方一律拦。
_USER_ONLY_BLOCKED = {"importlib"}
_HARD_BLOCKED = _DANGEROUS_ALWAYS_BLOCKED - _USER_ONLY_BLOCKED


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    # 区分调用方：只有用户代码（globals 的 __name__ 是 <askdata_code>）才受限制；
    # 白名单库（plotly/pandas 等）内部的惰性 import（例如 plotly 内部 import importlib）
    # 模块名由库自身决定、用户无法控制，予以放行，否则画个饼图都会被误伤。
    caller = "<unknown>"
    try:
        caller = sys._getframe(1).f_globals.get("__name__", "")
    except Exception:
        pass
    from_user_code = caller == "<askdata_code>"
    if root in _HARD_BLOCKED:
        # 硬危险模块（ctypes/subprocess/pickle 等逃逸终点）：库内部传递性导入也拦，
        # 例如用户代码 import numpy.ctypeslib 会让 numpy 内部 import ctypes
        raise ImportError(f"沙箱：禁止导入 {name!r}（危险模块）")
    if root in _USER_ONLY_BLOCKED and from_user_code:
        raise ImportError(f"沙箱：禁止导入 {name!r}（危险模块）")
    if root not in ALLOWED_TOP_LEVEL and from_user_code:
        raise ImportError(
            f"沙箱：禁止导入 {name!r}（仅允许 pandas/numpy/matplotlib/plotly 等白名单库）"
        )
    return _real_import(name, globals, locals, fromlist, level)


import builtins as _builtins  # noqa: E402

safe_builtins = dict(vars(_builtins))
for _name in ("exec", "eval", "compile", "input", "breakpoint", "help", "exit", "quit"):
    safe_builtins[_name] = _deny(f"沙箱：禁止调用 {_name}")
safe_builtins["__import__"] = _guarded_import
safe_builtins["open"] = _guarded_open

# 守卫 getattr：即使用字符串拼接绕过静态扫描去拿 dunder 属性，
# 运行时也会在这里被拦下（封死 __globals__ / __subclasses__ / __mro__ 等逃逸）。
_real_getattr = getattr
_BANNED_ATTR_NAMES = {
    "__globals__", "__builtins__", "__subclasses__", "__class__", "__bases__",
    "__mro__", "__code__", "__dict__", "__import__", "__reduce__", "__reduce_ex__",
    "__getstate__", "__setstate__", "__loader__", "__spec__", "__module__",
    "__func__", "__self__", "__closure__", "__defaults__", "__kwdefaults__",
    "__wrapped__", "__getattribute__", "__setattr__", "__delattr__", "__new__",
    "__init_subclass__",
}


def _guarded_getattr(obj, name, *args):
    if isinstance(name, str) and name in _BANNED_ATTR_NAMES:
        raise AttributeError(f"沙箱：禁止访问属性 {name!r}")
    return _real_getattr(obj, name, *args)


safe_builtins["getattr"] = _guarded_getattr

# 执行期间同时替换「真实」builtins，使 pandas/matplotlib 内部的 open() 也受同一道护栏约束
_orig_open = _builtins.open
_orig_import = _builtins.__import__


# ---------------------------------------------------------------
# 结果协议：save_result / save_chart 是暴露给 LLM 代码的助手函数
# ---------------------------------------------------------------
RESULTS: list[dict] = []
CHARTS: list[dict] = []


def _json_clean(obj, _pd=pd, _np=np):
    """递归转成 JSON 安全的原生类型。

    覆盖三类真实坑：
    - pandas.Timestamp / NaT（describe(include="all")、head 日期列都会带出来）；
    - numpy 标量（int64/float64/bool_ 不能直接 json.dump）；
    - NaN/Inf（标准 JSON 没有这些 token，前端 JSON.parse 会失败）-> None。
    """
    if obj is None:
        return None
    if isinstance(obj, _pd.Timestamp):
        return None if _pd.isna(obj) else obj.isoformat()
    if isinstance(obj, _np.integer):
        return int(obj)
    if isinstance(obj, _np.floating):
        value = float(obj)
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if isinstance(obj, _np.bool_):
        return bool(obj)
    if isinstance(obj, _np.ndarray):
        return [_json_clean(v) for v in obj.tolist()]
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float("inf"), float("-inf"))) else obj
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    return obj


def _json_default(o, _pd=pd):
    """json.dump 最后兜底：未知类型尽量取值，实在不行转字符串，绝不让进程裸崩。"""
    if isinstance(o, _pd.Timestamp):
        return None if _pd.isna(o) else o.isoformat()
    item = getattr(o, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(o)


def _save_result(value, description="", max_rows=200, _pd=pd, _np=np, _clean=_json_clean, _results=RESULTS):
    """把结果注册进返回结构：DataFrame/Series → 表格，标量/列表/字典 → 值。

    依赖通过默认参数绑定而非模块全局查找，配合下方 `types.FunctionType` 重挂
    `__globals__`，使拿到的函数对象无法回溯到 runner 模块命名空间。
    """
    columns = None
    if isinstance(value, _pd.DataFrame):
        kind = "table"
        columns = [str(c) for c in value.columns]
        data = value.head(max_rows).to_dict(orient="records")
    elif isinstance(value, _pd.Series):
        kind = "table"
        frame = value.reset_index()
        frame.columns = [str(c) if c != 0 else "value" for c in frame.columns]
        columns = [str(c) for c in frame.columns]
        data = frame.head(max_rows).to_dict(orient="records")
    elif isinstance(value, _np.ndarray):
        if value.ndim == 2:
            kind = "table"
            columns = [f"col{i}" for i in range(value.shape[1])]
            data = [dict(zip(columns, row)) for row in value[:max_rows].tolist()]
        else:
            kind = "value"
            data = value.tolist()
    elif isinstance(value, _np.generic):
        kind = "value"
        data = value.item()
    elif isinstance(value, (dict, list, tuple)):
        kind = "value"
        data = value
    else:
        kind = "value"
        data = str(value)
    data = _clean(data)
    _results.append(
        {
            "kind": kind,
            "description": description or "",
            "columns": columns,
            "data": data,
        }
    )
    return data


def _save_chart(fig=None, name="chart", dpi=110, _plt=plt, _chars=CHARTS, _chart_dir=CHART_DIR, _sep=os.sep):
    """保存图表到沙箱 charts 目录，返回文件名。

    支持 matplotlib Figure（PNG）与 plotly Figure（JSON，前端用 plotly.js 渲染）。
    """
    name = str(name)
    is_plotly = fig is not None and hasattr(fig, "write_json")
    if not name.lower().endswith((".png", ".json")):
        name = name + (".json" if is_plotly else ".png")
    path = _chart_dir + _sep + name
    if is_plotly:
        fig.write_json(path)
    else:
        fig = fig or _plt.gcf()
        fig.savefig(path, bbox_inches="tight", dpi=dpi, facecolor="white")
        _plt.close(fig)
    _chars.append({"file": name, "format": "plotly" if path.endswith(".json") else "png"})
    return name


# 用最小化的干净 globals 重新挂载两个助手函数，切断 `func.__globals__` 逃逸通道。
_HELPER_GLOBALS = {"__builtins__": safe_builtins, "__name__": "<askdata_helper>"}

save_result = types.FunctionType(
    _save_result.__code__, _HELPER_GLOBALS, "save_result",
    _save_result.__defaults__, _save_result.__closure__,
)
save_chart = types.FunctionType(
    _save_chart.__code__, _HELPER_GLOBALS, "save_chart",
    _save_chart.__defaults__, _save_chart.__closure__,
)


# ---------------------------------------------------------------
# 阶段 3：加载数据并执行用户代码
# ---------------------------------------------------------------
code_path, data_path, result_path = sys.argv[1:4]

_builtins.__import__ = _guarded_import
_builtins.open = _guarded_open

try:
    if data_path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(data_path)
    else:
        df = pd.read_csv(data_path)
    # 与数据预览保持一致：把名称含日期/时间的文本列转成 datetime，方便按月聚合
    for _col in df.columns:
        _key = str(_col).lower()
        if any(_k in _key for _k in ("date", "日期", "time", "时间")):
            try:
                _converted = pd.to_datetime(df[_col], errors="raise")
                if _converted.notna().mean() > 0.8:
                    df[_col] = _converted
            except Exception:
                continue
except Exception as exc:
    df = None
    RESULTS.append(
        {
            "kind": "value",
            "description": "数据加载失败",
            "columns": None,
            "data": f"{type(exc).__name__}: {exc}",
        }
    )

code = open(code_path, encoding="utf-8").read()  # 护栏内的文件读取

globals_dict = {
    "__name__": "<askdata_code>",
    "__builtins__": safe_builtins,
    "df": df,
    "pd": pd,
    "np": np,
    "plt": plt,
    "px": px,
    "go": go,
    "save_result": save_result,
    "save_chart": save_chart,
}

error = None
stdout_buf = io.StringIO()
_old_stdout = sys.stdout
sys.stdout = stdout_buf
_t0 = time.perf_counter()
try:
    try:
        exec(compile(code, "<askdata_code>", "exec"), globals_dict)
    except SystemExit:
        pass
    except BaseException as exc:  # 捕获后仍返回结构化错误，而不是裸崩溃
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc().replace(SANDBOX_DIR, "<sandbox>"),
        }
finally:
    sys.stdout = _old_stdout
    _builtins.open = _orig_open
    _builtins.__import__ = _orig_import

code_time_ms = int((time.perf_counter() - _t0) * 1000)

result = {
    "ok": error is None and df is not None,
    "error": error,
    "stdout": stdout_buf.getvalue()[:2000],
    "results": RESULTS,
    "charts": CHARTS,
    "code_time_ms": code_time_ms,
}
if df is None and error is None:
    result["error"] = {
        "type": "DataLoadError",
        "message": "数据文件加载失败",
        "traceback": "",
    }

with open(result_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, default=_json_default, allow_nan=False)

sys.exit(0)
