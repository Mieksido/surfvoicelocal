"""磁盘 LRU。键 = 所有影响音频字节的东西，命中即 0ms。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path


def make_key(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


class DiskCache:
    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / f"{key}.bin", self.root / f"{key}.json"

    def get(self, key: str):
        """返回 (pcm_bytes, meta) 或 None。"""
        binf, metaf = self._paths(key)
        try:
            pcm = binf.read_bytes()
            meta = json.loads(metaf.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        # 触碰 mtime，LRU 才对
        try:
            os.utime(binf, None)
        except OSError:
            pass
        return pcm, meta

    def put(self, key: str, pcm: bytes, meta: dict) -> None:
        binf, metaf = self._paths(key)
        with self._lock:
            try:
                tmp = binf.with_suffix(".tmp")
                tmp.write_bytes(pcm)
                os.replace(tmp, binf)
                metaf.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
            except OSError:
                return
            self._prune()

    def _prune(self) -> None:
        files = []
        total = 0
        for p in self.root.iterdir():
            if p.suffix not in (".bin", ".json"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        if total <= self.max_bytes:
            return
        files.sort()
        for _, size, p in files:
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
            if total <= self.max_bytes:
                break

    def stats(self) -> dict:
        n = 0
        total = 0
        for p in self.root.glob("*.bin"):
            try:
                total += p.stat().st_size
                n += 1
            except OSError:
                pass
        return {"entries": n, "bytes": total, "max_bytes": self.max_bytes}
