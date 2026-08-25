"""AskData FastAPI 应用：上传预览 / 示例数据 / 聊天问答（LLM 生成代码 → 沙箱执行）。"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, sandbox
from .data_loader import build_preview, build_schema, read_table
from .llm import LLMError, generate_code
from .sample_data import save_sample_csv
from .sessions import SessionStore

config.ensure_dirs()
store = SessionStore(config.MEDIA_DIR)

app = FastAPI(
    title="AskData",
    description="自然语言数据分析助手：LLM 生成 pandas 代码，沙箱安全执行，返回表格与图表。",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = config.BASE_DIR / "static"
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}

SUGGESTED_QUESTIONS = [
    "按月统计销售额，画个趋势图",
    "哪个品类利润最高？画柱状图",
    "各地区销售额占比",
    "哪种渠道卖得最好",
    "哪个月利润最高",
]


class AskRequest(BaseModel):
    session_id: str
    question: str


def _safe_filename(name: str) -> str:
    name = Path(name or "upload.csv").name
    safe = "".join(c for c in name if c.isalnum() or c in "._-（）() ")
    return safe[:80] or "upload.csv"


def _session_payload(session_id: str, filename: str, df) -> dict:
    return {
        "session_id": session_id,
        "filename": filename,
        "row_count": int(df.shape[0]),
        "column_count": int(df.shape[1]),
        "columns": [str(c) for c in df.columns],
        "preview": build_preview(df),
        "schema": build_schema(df),
        "suggested_questions": SUGGESTED_QUESTIONS,
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "llm_provider": config.LLM_PROVIDER,
        "sandbox_timeout_s": config.SANDBOX_TIMEOUT_S,
        "sandbox_memory_mb": config.SANDBOX_MEMORY_MB,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持 .csv / .xlsx / .xls 文件")

    dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{_safe_filename(file.filename)}"
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过 {config.MAX_UPLOAD_MB}MB 上限",
                )
            out.write(chunk)

    try:
        df = read_table(dest)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise

    session_id = store.create(file.filename or dest.name, df, "upload", dest)
    return _session_payload(session_id, file.filename or dest.name, df)


@app.post("/api/sample")
def sample():
    if not config.SAMPLE_CSV.exists():
        save_sample_csv(config.SAMPLE_CSV)
    df = read_table(config.SAMPLE_CSV)
    session_id = store.create("sample_sales.csv", df, "sample", config.SAMPLE_CSV)
    return _session_payload(session_id, "sample_sales.csv", df)


def _build_reply(question: str, outcome: sandbox.SandboxOutcome) -> str:
    if outcome.ok:
        parts = [f"✅ 已分析「{question}」，沙箱安全执行成功（代码运行 {outcome.code_time_ms}ms）。"]
        if outcome.stdout.strip():
            parts.append(outcome.stdout.strip())
        return "\n".join(parts)
    if outcome.kind == "blocked":
        return f"🛡️ 代码未通过安全检查，已拒绝执行：\n{outcome.message}"
    return f"❌ 分析失败：{outcome.message}"


@app.post("/api/ask")
def ask(req: AskRequest):
    session = store.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传数据")
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")

    df = session.df
    schema = build_schema(df)
    preview = build_preview(df)
    try:
        code = generate_code(req.question, schema, int(df.shape[0]), preview)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix="askdata_charts_") as tmp:
        chart_src = Path(tmp) / "charts"
        outcome = sandbox.run_code(code, session.data_path, charts_out=chart_src)
        if outcome.ok:
            charts = store.save_charts(session.id, outcome.charts, chart_src)
        else:
            charts = []

    return {
        "question": req.question,
        "reply": _build_reply(req.question, outcome),
        "code": code,
        "results": outcome.results if outcome.ok else [],
        "charts": charts,
        "stdout": outcome.stdout,
        "sandbox": {
            "ok": outcome.ok,
            "kind": outcome.kind,
            "message": outcome.message,
            "duration_ms": outcome.duration_ms,
            "code_time_ms": outcome.code_time_ms,
            "memory_mb": outcome.memory_mb,
            "timeout_s": outcome.timeout_s,
        },
    }


app.mount("/media", StaticFiles(directory=config.MEDIA_DIR), name="media")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static_root")
