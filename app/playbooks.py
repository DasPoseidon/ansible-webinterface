"""Playbooks finden, pruefen und ihre geplanten Schritte auflisten."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .config import Config

MAX_PARSE_BYTES = 2 * 1024 * 1024
PLAY_KEYS = {"hosts", "import_playbook", "include"}


def _is_playbook(document: Any) -> bool:
    """Ein Playbook ist eine Liste von Plays bzw. eingebundenen Playbooks."""
    if not isinstance(document, list) or not document:
        return False
    for entry in document:
        if not isinstance(entry, dict):
            return False
        if not (set(entry) & PLAY_KEYS):
            return False
    return True


def _collect_tags(document: Any) -> List[str]:
    found: set = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "tags":
                    if isinstance(value, str):
                        found.update(part.strip() for part in value.split(",") if part.strip())
                    elif isinstance(value, list):
                        found.update(str(v) for v in value if isinstance(v, (str, int)))
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(document)
    return sorted(found)


def parse_list_tasks(text: str) -> Dict[str, Any]:
    """Wertet die Ausgabe von ``ansible-playbook --list-tasks`` aus."""
    plays: List[Dict[str, Any]] = []
    count = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        play = re.match(r"^play #\d+ \((.*?)\):\s*(.*?)\s*TAGS:\s*\[(.*?)\]\s*$", line)
        if play:
            plays.append({
                "pattern": play.group(1),
                "name": play.group(2),
                "tags": [t.strip() for t in play.group(3).split(",") if t.strip()],
                "tasks": [],
            })
            continue
        if line.startswith(("playbook:", "tasks:")) or not plays:
            continue
        task = re.match(r"^(.*?)\s+TAGS:\s*\[(.*?)\]\s*$", line)
        if task:
            count += 1
            if len(plays[-1]["tasks"]) < 500:
                plays[-1]["tasks"].append({
                    "name": task.group(1).strip(),
                    "tags": [t.strip() for t in task.group(2).split(",") if t.strip()],
                })
    return {"plays": plays, "task_count": count}


def parse_list_tags(text: str) -> List[str]:
    tags: set = set()
    for raw in (text or "").splitlines():
        match = re.search(r"TASK TAGS:\s*\[(.*?)\]", raw)
        if match:
            tags.update(t.strip() for t in match.group(1).split(",") if t.strip())
    return sorted(tags)


class PlaybookService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_key: Optional[Tuple] = None

    # -- Finden -----------------------------------------------------------

    def _excluded(self, path: Path) -> bool:
        excluded = set(self.cfg.discovery_cfg.get("exclude_dirs") or [])
        try:
            relative = path.relative_to(self.cfg.ansible_dir)
        except ValueError:
            return True
        parts = relative.parts[:-1]
        if any(part in excluded or part.startswith(".") for part in parts):
            return True
        if path.name.startswith("."):
            return True
        # Im Daten-/Konfigordner liegen Datendateien, keine startbaren
        # Playbooks. Ist dort aber das Ansible-Verzeichnis selbst eingetragen
        # (data_dir: "."), darf das nicht alles ausschliessen.
        try:
            if (self.cfg.data_dir != self.cfg.ansible_dir
                    and self.cfg.data_dir in path.parents):
                return True
        except (OSError, ValueError):
            pass
        return False

    def _candidates(self) -> List[Path]:
        found: Dict[str, Path] = {}
        for pattern in self.cfg.discovery_cfg.get("playbook_globs") or []:
            try:
                for path in self.cfg.ansible_dir.glob(pattern):
                    if path.is_file() and not self._excluded(path):
                        found[str(path)] = path
            except (OSError, ValueError):
                continue
        return sorted(found.values())

    def _key(self, paths: List[Path]) -> Tuple:
        items = []
        for path in paths:
            try:
                stat = path.stat()
                items.append((str(path), stat.st_mtime, stat.st_size))
            except OSError:
                continue
        return tuple(items)

    def discover(self, force: bool = False) -> List[Dict[str, Any]]:
        paths = self._candidates()
        key = self._key(paths)
        if not force and self._cache is not None and self._cache_key == key:
            return self._cache
        result: List[Dict[str, Any]] = []
        for path in paths:
            info = self._inspect(path)
            if info is not None:
                result.append(info)
        result.sort(key=lambda item: item["path"])
        self._cache = result
        self._cache_key = key
        return result

    def _inspect(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            stat = path.stat()
        except OSError:
            return None
        if stat.st_size > MAX_PARSE_BYTES:
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # Kaputte Playbooks trotzdem anzeigen, damit man sie reparieren kann
            if re.search(r"^\s*-\s+(hosts|import_playbook):", text, re.MULTILINE):
                return {
                    "path": str(path.relative_to(self.cfg.ansible_dir)),
                    "name": path.stem,
                    "plays": [],
                    "play_count": 0,
                    "task_count": None,
                    "tags": [],
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "valid": False,
                    "error": "YAML-Fehler: %s" % str(exc).splitlines()[0],
                }
            return None
        if not _is_playbook(document):
            return None

        plays: List[Dict[str, Any]] = []
        task_count = 0
        for entry in document:
            if "import_playbook" in entry or "include" in entry:
                plays.append({
                    "name": "Playbook einbinden: %s" % (entry.get("import_playbook") or entry.get("include")),
                    "hosts": "",
                    "imported": True,
                })
                continue
            hosts = entry.get("hosts")
            if isinstance(hosts, (list, tuple)):
                hosts = ",".join(str(h) for h in hosts)
            counted = 0
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                block = entry.get(section)
                if isinstance(block, list):
                    counted += len(block)
            roles = entry.get("roles")
            plays.append({
                "name": entry.get("name") or str(hosts or "(ohne Namen)"),
                "hosts": str(hosts or ""),
                "task_count": counted,
                "role_count": len(roles) if isinstance(roles, list) else 0,
                "serial": entry.get("serial"),
                "imported": False,
            })
            task_count += counted
        return {
            "path": str(path.relative_to(self.cfg.ansible_dir)),
            "name": (plays[0]["name"] if plays else path.stem),
            "plays": plays,
            "play_count": len(plays),
            "task_count": task_count,
            "tags": _collect_tags(document),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "valid": True,
            "error": None,
        }

    # -- Pruefen ----------------------------------------------------------

    async def _run_ansible(self, args: List[str], timeout: float = 120) -> Tuple[int, str, str]:
        argv = [self.cfg.ansible_playbook, "-i", str(self.cfg.inventory)] + args
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.cfg.ansible_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            return 124, "", "Zeitueberschreitung nach %ds" % timeout
        except OSError as exc:
            return 127, "", "ansible-playbook konnte nicht gestartet werden: %s" % exc
        return (process.returncode or 0,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"))

    async def syntax_check(self, playbook: str) -> Dict[str, Any]:
        rc, stdout, stderr = await self._run_ansible(["--syntax-check", playbook], timeout=120)
        return {
            "ok": rc == 0,
            "rc": rc,
            "output": (stderr.strip() + ("\n" + stdout.strip() if stdout.strip() else "")).strip(),
        }

    async def list_tasks(self, playbook: str, *, limit: str = "", tags: Optional[List[str]] = None,
                         skip_tags: Optional[List[str]] = None) -> Dict[str, Any]:
        args = ["--list-tasks", playbook]
        if limit.strip():
            args += ["--limit", limit.strip()]
        if tags:
            args += ["--tags", ",".join(tags)]
        if skip_tags:
            args += ["--skip-tags", ",".join(skip_tags)]
        rc, stdout, stderr = await self._run_ansible(args, timeout=120)
        if rc != 0:
            return {"ok": False, "error": (stderr.strip() or stdout.strip())[:4000],
                    "plays": [], "task_count": 0}
        parsed = parse_list_tasks(stdout)
        parsed.update({"ok": True, "error": None})
        return parsed

    async def list_tags(self, playbook: str) -> Dict[str, Any]:
        rc, stdout, stderr = await self._run_ansible(["--list-tags", playbook], timeout=120)
        if rc != 0:
            return {"ok": False, "tags": [], "error": (stderr.strip() or stdout.strip())[:2000]}
        return {"ok": True, "tags": parse_list_tags(stdout), "error": None}

    async def list_hosts(self, playbook: str, limit: str = "") -> Dict[str, Any]:
        args = ["--list-hosts", playbook]
        if limit.strip():
            args += ["--limit", limit.strip()]
        rc, stdout, stderr = await self._run_ansible(args, timeout=120)
        if rc != 0:
            return {"ok": False, "hosts": [], "error": (stderr.strip() or stdout.strip())[:2000]}
        hosts: List[str] = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line or line.startswith(("playbook:", "pattern:", "hosts ")) or "(" in line and ":" in line:
                continue
            if re.match(r"^[A-Za-z0-9_.:\-\[\]]+$", line):
                hosts.append(line)
        return {"ok": True, "hosts": sorted(set(hosts)), "error": None}
