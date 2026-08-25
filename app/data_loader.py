"""CSV / Excel 加载、字段推断与预览。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from fastapi import HTTPException

from . import config

DTYPE_KIND_MAP = {
    "object": "文本",
    "int": "整数",
    "float": "小数",
    "bool": "布尔",
    "datetime": "日期",
    "timedelta": "时间差",
}


def _infer_kind(dtype: str) -> str:
    if dtype == "object":
        return "文本"
    for prefix, label in DTYPE_KIND_MAP.items():
        if dtype.startswith(prefix):
            return label
    return dtype


def _smart_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """把名称含 日期/date/time 的文本列尝试转成 datetime，方便按月聚合。"""
    for col in df.columns:
        key = str(col).lower()
        if any(k in key for k in ("date", "日期", "time", "时间")):
            try:
                converted = pd.to_datetime(df[col], errors="raise")
                if converted.notna().mean() > 0.8:
                    df[col] = converted
            except Exception:
                continue
    return df


def read_table(path: str | Path) -> pd.DataFrame:
    """读取 CSV（自动尝试常见编码）或 Excel，返回清洗后的 DataFrame。"""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        else:
            for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
                try:
                    df = pd.read_csv(path, encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                raise HTTPException(status_code=400, detail="无法识别 CSV 编码（尝试了 utf-8 / gbk）")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"文件解析失败：{exc}") from exc

    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        raise HTTPException(status_code=400, detail="文件里没有有效数据行")
    if df.shape[1] == 0:
        raise HTTPException(status_code=400, detail="文件里没有有效列")
    if df.shape[0] > config.MAX_ROWS:
        raise HTTPException(status_code=400, detail=f"数据超过 {config.MAX_ROWS} 行限制")
    if df.shape[1] > config.MAX_COLS:
        raise HTTPException(status_code=400, detail=f"数据超过 {config.MAX_COLS} 列限制")
    return _smart_dtypes(df)


def build_schema(df: pd.DataFrame) -> list[dict]:
    """生成字段说明：类型 / 非空 / 去重 / 示例值，喂给 LLM 和前端。"""
    schema = []
    for col in df.columns:
        series = df[col]
        dtype = str(series.dtype)
        sample = []
        for value in series.dropna().head(3).tolist():
            if hasattr(value, "strftime"):
                sample.append(value.strftime("%Y-%m-%d"))
            else:
                sample.append(value)
        schema.append(
            {
                "name": str(col),
                "dtype": dtype,
                "kind": _infer_kind(dtype),
                "non_null": int(series.notna().sum()),
                "unique": int(series.nunique(dropna=True)),
                "missing": int(series.isna().sum()),
                "sample": sample,
            }
        )
    return schema


def build_preview(df: pd.DataFrame, n: int = 10) -> list[dict]:
    """前 n 行预览（JSON 安全，中文保留，日期转 ISO）。"""
    records = json.loads(
        df.head(n).to_json(orient="records", force_ascii=False, date_format="iso")
    )
    return records


def columns_of(df: pd.DataFrame) -> list[str]:
    return [str(c) for c in df.columns]

