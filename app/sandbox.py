"""沙箱编排（父进程侧）。

安全链路（纵深防御，五层）：
1. AST 静态扫描：先拒绝明显危险代码（危险导入 / exec / open / dunder 逃逸）；
2. 独立 subprocess + Python 隔离模式（-I），与主服务进程隔离；
3. 运行时护栏（在 sandbox_runner.py 内）：白名单导入、断网、文件读写限定、禁用危险内建；
4. 超时控制：等 runner 就绪后开始计时，超时整树杀掉；
5. 内存限制：Linux 用 rlimit 硬限制，全平台用 psutil 看门狗兜底。
"""

from __future__ import annotations

import ast
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config

BANNED_IMPORTS = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "pickle",
    "marshal", "ctypes", "importlib", "builtins", "multiprocessing",
    "requests", "urllib", "http", "ftplib", "smtplib", "paramiko",
    "sqlite3", "zipfile", "tarfile", "gzip", "webbrowser", "pty",
    "resource", "signal", "platform", "pdb", "ssl", "asyncio", "concurrent",
    "email", "xml", "xmlrpc", "bz2", "lzma", "zlib",
}
BANNED_CALLS = {
    "exec", "eval", "compile", "open", "input", "breakpoint",
    "__import__", "help", "exit", "quit",
}
BANNED_ATTRS = {
    "__globals__", "__builtins__", "__subclasses__", "__class__",
    "__bases__", "__mro__", "__code__", "__dict__", "__import__",
    "__reduce__", "__reduce_ex__", "__getstate__", "__setstate__",
    "__loader__", "__spec__", "__module__",
}


def static_check(code: str) -> list[str]:
    """第一层防线：AST 静态扫描，返回违规列表（空列表 = 通过）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"代码语法错误：{exc}"]

    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    issues.append(f"第 {node.lineno} 行：禁止导入 {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS:
                issues.append(f"第 {node.lineno} 行：禁止导入 {node.module!r}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                issues.append(f"第 {node.lineno} 行：禁止调用 {node.func.id}()")
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                issues.append(f"第 {node.lineno} 行：禁止访问 .{node.attr}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and str(node.args[1].value).startswith("__"):
                issues.append(f"第 {node.lineno} 行：禁止通过 getattr 访问 dunder 属性")
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "__builtins__":
            issues.append(f"第 {node.lineno} 行：禁止直接访问 __builtins__")
    return issues


@dataclass
class ChartAsset:
    file: str
    format: str


@dataclass
class SandboxOutcome:
    ok: bool = False
    kind: str = "error"  # result | blocked | timeout | memory | startup_error | error
    message: str = ""
    stdout: str = ""
    results: list = field(default_factory=list)
    charts: list[ChartAsset] = field(default_factory=list)
    duration_ms: int = 0
    code_time_ms: int = 0
    memory_mb: int = config.SANDBOX_MEMORY_MB
    timeout_s: float = config.SANDBOX_TIMEOUT_S
    error: dict | None = None


def _child_env(tmp_dir: Path) -> dict[str, str]:
    """构造最小化环境：剔除密钥与代理，只保留沙箱必要变量。"""
    env = {
        "PYTHONIOENCODING": "utf-8",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(tmp_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "ASKDATA_SANDBOX_DIR": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
    }
    for key, value in os.environ.items():
        low = key.lower()
        if "key" in low or "token" in low or "secret" in low or "password" in low or "proxy" in low or "cookie" in low:
            continue
        if key.startswith("ASKDATA_"):
            continue
        env[key] = value
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉进程树（含子进程），Windows/Linux 通用。"""
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


class _MemoryWatchdog(threading.Thread):
    """轮询子进程内存（含子进程树），超限即杀。"""

    def __init__(self, proc: subprocess.Popen, limit_mb: int):
        super().__init__(daemon=True)
        self.proc = proc
        self.limit = limit_mb * 1024 * 1024
        self.exceeded = False
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            import psutil

            parent = psutil.Process(self.proc.pid)
        except Exception:
            return
        while not self._stop.wait(0.1):
            try:
                total = parent.memory_info().rss + sum(
                    c.memory_info().rss for c in parent.children(recursive=True)
                )
            except Exception:
                continue
            if total > self.limit:
                self.exceeded = True
                _kill_tree(self.proc)
                return


def _wait_ready(proc: subprocess.Popen, startup_timeout: float) -> tuple[bool, list[str]]:
    """等待 runner 打印 READY（表示依赖加载完毕），返回是否就绪及已收集的 stderr。"""
    lines: list[str] = []
    q: queue.Queue[str] = queue.Queue()

    def reader():
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            lines.append(line)
            q.put(line)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if line == "READY":
            return True, lines, thread
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break
    return False, lines, thread


def _posix_limit_fn(timeout_s: float, memory_mb: int):
    """Linux 硬限制：虚拟内存 + CPU 时间（兜底防失控）。"""
    import resource

    def _apply():
        bytes_limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s) + 30, int(timeout_s) + 30))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _apply


def run_code(
    code: str,
    data_path: str | Path,
    charts_out: str | Path | None = None,
    timeout_s: float | None = None,
    memory_mb: int | None = None,
) -> SandboxOutcome:
    """执行 LLM 生成的代码，返回结构化结果。"""
    timeout_s = timeout_s if timeout_s is not None else config.SANDBOX_TIMEOUT_S
    memory_mb = memory_mb if memory_mb is not None else config.SANDBOX_MEMORY_MB
    outcome = SandboxOutcome(memory_mb=memory_mb, timeout_s=timeout_s)
    start = time.perf_counter()

    # 第一层：静态扫描
    issues = static_check(code)
    if issues:
        outcome.kind = "blocked"
        outcome.message = "静态扫描未通过，已拒绝执行：\n- " + "\n- ".join(issues[:8])
        return outcome

    runner = Path(__file__).with_name("sandbox_runner.py")
    with tempfile.TemporaryDirectory(prefix="askdata_") as tmp_raw:
        tmp = Path(tmp_raw)
        (tmp / "charts").mkdir(exist_ok=True)
        data_copy = tmp / ("data" + Path(data_path).suffix.lower() or ".csv")
        shutil.copy2(data_path, data_copy)
        code_file = tmp / "code.py"
        code_file.write_text(code, encoding="utf-8")
        result_file = tmp / "result.json"

        preexec = _posix_limit_fn(timeout_s, memory_mb) if os.name == "posix" else None
        proc = subprocess.Popen(
            [
                sys.executable, "-X", "utf8", "-I",
                str(runner), str(code_file), str(data_copy), str(result_file),
            ],
            cwd=tmp,
            env=_child_env(tmp),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=preexec,
        )

        ready, stderr_lines, reader_thread = _wait_ready(proc, config.SANDBOX_STARTUP_TIMEOUT_S)
        if not ready:
            _kill_tree(proc)
            outcome.kind = "startup_error"
            outcome.message = "沙箱启动失败（依赖加载超时或异常）"
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            if stderr_lines:
                reader_thread.join(timeout=1.0)
                tail = [l for l in stderr_lines if l != "READY"]
                outcome.message += "\n".join([""] + tail[-15:]) if tail else outcome.message
            return outcome

        # 就绪后才开始计时：5 秒只算「用户代码执行」，不含依赖加载
        watchdog = _MemoryWatchdog(proc, memory_mb)
        watchdog.start()
        code_start = time.perf_counter()
        try:
            proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            watchdog.stop()
            outcome.kind = "timeout"
            outcome.message = (
                f"代码执行超过 {timeout_s:g}s，已强制终止"
                "（可能是死循环或数据量过大，试试更聚焦的写法）"
            )
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            return outcome
        watchdog.stop()
        outcome.code_time_ms = int((time.perf_counter() - code_start) * 1000)
        outcome.duration_ms = int((time.perf_counter() - start) * 1000)

        if watchdog.exceeded:
            outcome.kind = "memory"
            outcome.message = f"内存占用超过 {memory_mb}MB 限制，已强制终止"
            return outcome

        if proc.returncode != 0:
            outcome.kind = "error"
            outcome.message = f"执行进程异常退出（exit code={proc.returncode}）"
            if stderr_lines:
                reader_thread.join(timeout=1.0)
                tail = [l for l in stderr_lines if l != "READY"]
                outcome.message += "\n".join([""] + tail[-15:]) if tail else outcome.message
            return outcome

        if not result_file.exists():
            outcome.kind = "error"
            outcome.message = "沙箱未产出结果文件"
            return outcome

        import json

        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception as exc:
            outcome.kind = "error"
            outcome.message = f"结果解析失败：{exc}"
            return outcome

        outcome.ok = bool(payload.get("ok"))
        outcome.error = payload.get("error")
        outcome.stdout = payload.get("stdout", "")
        outcome.results = payload.get("results", [])
        outcome.code_time_ms = payload.get("code_time_ms", outcome.code_time_ms)
        if outcome.ok:
            outcome.kind = "result"
            outcome.message = "执行成功"
            for chart in payload.get("charts", []):
                outcome.charts.append(ChartAsset(file=chart["file"], format=chart["format"]))
            if charts_out:
                dest = Path(charts_out)
                dest.mkdir(parents=True, exist_ok=True)
                src = tmp / "charts"
                for chart in outcome.charts:
                    src_file = src / chart.file
                    if src_file.exists():
                        shutil.copy2(src_file, dest / chart.file)
        else:
            outcome.kind = "error"
            detail = (outcome.error or {}).get("message", "未知错误")
            outcome.message = f"代码运行出错（{detail}）"
        return outcome
