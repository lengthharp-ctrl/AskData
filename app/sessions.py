"""内存会话管理：MVP 不引入数据库，数据帧驻留内存，图表落盘到媒体目录。"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import config


@dataclass
class Session:
    id: str
    filename: str
    source: str  # upload | sample
    df: pd.DataFrame
    data_path: Path
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, media_dir: Path):
        self._sessions: dict[str, Session] = {}
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        threading.Thread(target=self._cleaner_loop, daemon=True).start()

    def create(self, filename: str, df: pd.DataFrame, source: str, data_path: Path) -> str:
        session_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._sessions[session_id] = Session(
                id=session_id,
                filename=filename,
                source=source,
                df=df,
                data_path=data_path,
            )
        return session_id

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.last_used = time.time()
            return session

    def media_dir_for(self, session_id: str) -> Path:
        return self.media_dir / session_id / "charts"

    def save_charts(self, session_id: str, charts: list, src_dir: Path) -> list[dict]:
        """把沙箱产出的图表复制到会话媒体目录，返回可访问 URL。"""
        dest = self.media_dir_for(session_id)
        dest.mkdir(parents=True, exist_ok=True)
        urls = []
        for chart in charts:
            src_file = src_dir / chart.file
            if src_file.exists():
                shutil.copy2(src_file, dest / chart.file)
            urls.append(
                {
                    "file": chart.file,
                    "format": chart.format,
                    "url": f"/media/{session_id}/charts/{chart.file}",
                }
            )
        return urls

    def _cleaner_loop(self) -> None:
        """定期清理过期会话与对应媒体目录，防止内存 / 磁盘无限增长。"""
        while True:
            time.sleep(600)
            now = time.time()
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_used > config.SESSION_TTL_MINUTES * 60
            ]
            for sid in expired:
                with self._lock:
                    self._sessions.pop(sid, None)
                shutil.rmtree(self.media_dir / sid, ignore_errors=True)

