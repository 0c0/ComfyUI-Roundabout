"""response_format=url 时的临时产物存储（本地磁盘 + TTL 清理）。"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

from .config import settings

log = logging.getLogger("roundabout.store")


class OutputStore:
    def __init__(self, directory: Path, ttl: float) -> None:
        self.dir = directory
        self.ttl = ttl
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, ext: str = "png") -> str:
        name = f"{uuid.uuid4().hex}.{ext.lstrip('.')}"
        (self.dir / name).write_bytes(data)
        return name

    def path(self, name: str) -> Path | None:
        # 防目录穿越：只接受纯文件名
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        p = (self.dir / name).resolve()
        if not str(p).startswith(str(self.dir.resolve())):
            return None
        return p if p.is_file() else None

    def sweep(self) -> int:
        if self.ttl <= 0:
            return 0
        cutoff = time.time() - self.ttl
        removed = 0
        for f in self.dir.glob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    os.remove(f)
                    removed += 1
            except OSError as exc:  # noqa: PERF203
                log.debug("sweep failed for %s: %s", f, exc)
        if removed:
            log.info("swept %d expired output file(s)", removed)
        return removed


store = OutputStore(settings.output_dir, settings.output_ttl)
