"""Audit-Log: wer hat was wann getan."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, user: str, action: str, target: str = "", *, ok: bool = True,
              detail: Optional[Dict[str, Any]] = None, ip: str = "") -> None:
        entry = {
            "ts": time.time(),
            "user": user or "-",
            "action": action,
            "target": target,
            "ok": bool(ok),
            "ip": ip,
        }
        if detail:
            entry["detail"] = detail
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        backup = self.path.with_name(self.path.name + ".1")
        try:
            os.replace(str(self.path), str(backup))
        except OSError:
            pass

    def tail(self, limit: int = 300, *, user: Optional[str] = None,
             action: Optional[str] = None) -> List[Dict[str, Any]]:
        """Liest die letzten Eintraege, neueste zuerst."""
        limit = max(1, min(int(limit), 5000))
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                chunk = min(size, 2 * 1024 * 1024)
                handle.seek(size - chunk)
                data = handle.read().decode("utf-8", "replace")
        except OSError:
            return []
        entries: List[Dict[str, Any]] = []
        for raw in reversed(data.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if user and entry.get("user") != user:
                continue
            if action and action not in str(entry.get("action", "")):
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
        return entries
