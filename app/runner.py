"""Ausfuehrung von Playbooks: Warteschlange, Live-Zustand, Abbruch.

Playbooks werden als normaler Subprozess im echten Ansible-Verzeichnis
gestartet. Damit gelten ``ansible.cfg``, Rollenpfade, relative Pfade und
SSH-Einstellungen genau so wie bei einem Aufruf per Hand oder aus Cron.
Der Live-Zustand entsteht aus den Ereignissen des Callback-Plugins.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import signal
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .bus import Broadcaster
from .config import Config
from .playbooks import parse_list_tasks

QUEUED = "queued"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
ERROR = "error"

ACTIVE_STATES = (QUEUED, RUNNING)
LOG_TAIL_LINES = 1000
MAX_TRACKED_TASKS = 4000

# Ansible-Rueckgabecodes
RC_MEANING = {
    0: "Erfolgreich",
    1: "Allgemeiner Fehler",
    2: "Mindestens ein Host ist fehlgeschlagen",
    3: "Mindestens ein Host war nicht erreichbar",
    4: "Fehler beim Einlesen (Parser)",
    5: "Ungueltige Optionen",
    99: "Durch Benutzer abgebrochen",
    250: "Unerwarteter Fehler in Ansible",
}


class RunError(Exception):
    """Ein Lauf konnte nicht angelegt oder gestartet werden."""


@dataclass
class RunRequest:
    playbook: str
    limit: str = ""
    tags: List[str] = field(default_factory=list)
    skip_tags: List[str] = field(default_factory=list)
    check: bool = False
    diff: bool = False
    extra_vars: str = ""
    verbosity: int = 0
    forks: Optional[int] = None
    user: str = "-"
    source: str = "web"
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RunState:
    """Aggregierter Live-Zustand eines Laufs."""

    def __init__(self) -> None:
        self.plays: List[Dict[str, Any]] = []
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.task_order: List[str] = []
        self.hosts: Dict[str, Dict[str, Any]] = {}
        self.stats: Dict[str, Any] = {}
        self.notices: List[str] = []
        self.current_play: Optional[str] = None
        self.current_task: Optional[str] = None
        self.truncated = False

    # -- Hilfen -----------------------------------------------------------

    def _host(self, name: str) -> Dict[str, Any]:
        host = self.hosts.get(name)
        if host is None:
            host = {
                "name": name,
                "state": "wartet",
                "current_task": None,
                "current_task_name": "",
                "counts": {"ok": 0, "changed": 0, "failed": 0, "skipped": 0, "unreachable": 0},
                "last_duration": None,
                "last_error": None,
                "started_at": None,
                "updated_at": None,
            }
            self.hosts[name] = host
        return host

    def _register_hosts(self, names: List[str]) -> None:
        for name in names:
            self._host(name)

    # -- Ereignisverarbeitung --------------------------------------------

    def apply(self, event: Dict[str, Any]) -> None:
        kind = event.get("e")
        handler = getattr(self, "_ev_" + str(kind), None)
        if handler is not None:
            try:
                handler(event)
            except Exception as exc:                       # pragma: no cover
                self.notices.append("Ereignis %s nicht verarbeitet: %s" % (kind, exc))

    def _ev_play_start(self, event: Dict[str, Any]) -> None:
        play = {
            "uuid": event.get("play_uuid", ""),
            "name": event.get("name") or "(ohne Namen)",
            "pattern": event.get("pattern") or [],
            "hosts": event.get("hosts") or [],
            "check_mode": bool(event.get("check_mode")),
            "tasks": [],
            "started_at": event.get("t"),
        }
        self.plays.append(play)
        self.current_play = play["uuid"]
        self._register_hosts(play["hosts"])

    def _ev_task_start(self, event: Dict[str, Any]) -> None:
        uuid_ = event.get("task_uuid", "")
        if uuid_ in self.tasks:
            return
        if len(self.task_order) >= MAX_TRACKED_TASKS:
            self.truncated = True
            return
        task = {
            "uuid": uuid_,
            "play_uuid": event.get("play_uuid") or self.current_play,
            "name": event.get("name") or event.get("action") or "(Task)",
            "action": event.get("action") or "",
            "path": event.get("path") or "",
            "is_handler": bool(event.get("is_handler")),
            "no_log": bool(event.get("no_log")),
            "order": len(self.task_order),
            "started_at": event.get("t"),
            "running": [],
            "results": {},
            "counts": {"ok": 0, "changed": 0, "failed": 0, "skipped": 0, "unreachable": 0},
        }
        self.tasks[uuid_] = task
        self.task_order.append(uuid_)
        self.current_task = uuid_
        for play in self.plays:
            if play["uuid"] == task["play_uuid"]:
                play["tasks"].append(uuid_)
                break

    def _ev_host_task_start(self, event: Dict[str, Any]) -> None:
        name = event.get("host", "?")
        host = self._host(name)
        task_uuid = event.get("task_uuid", "")
        host["state"] = "laeuft"
        host["current_task"] = task_uuid
        host["current_task_name"] = event.get("task_name") or ""
        host["updated_at"] = event.get("t")
        if host["started_at"] is None:
            host["started_at"] = event.get("t")
        task = self.tasks.get(task_uuid)
        if task is not None and name not in task["running"]:
            task["running"].append(name)

    def _ev_host_result(self, event: Dict[str, Any]) -> None:
        name = event.get("host", "?")
        status = event.get("status", "ok")
        host = self._host(name)
        task_uuid = event.get("task_uuid", "")
        data = event.get("data") or {}
        ignored = bool(event.get("ignore_errors"))

        key = status if status in host["counts"] else "ok"
        if status == "failed" and ignored:
            key = "ok"
        host["counts"][key] = host["counts"].get(key, 0) + 1
        host["last_duration"] = event.get("duration")
        host["updated_at"] = event.get("t")
        host["current_task"] = None
        host["current_task_name"] = ""
        if status in ("failed", "unreachable") and not ignored:
            host["state"] = status
            host["last_error"] = str(data.get("msg") or data.get("stderr") or data.get("reason") or "")[:2000]
        elif host["state"] not in ("failed", "unreachable"):
            host["state"] = "bereit"

        task = self.tasks.get(task_uuid)
        if task is not None:
            if name in task["running"]:
                task["running"].remove(name)
            task["counts"][key] = task["counts"].get(key, 0) + 1
            task["results"][name] = {
                "status": status,
                "ignored": ignored,
                "duration": event.get("duration"),
                "msg": str(data.get("msg") or "")[:2000] or None,
                "rc": data.get("rc"),
                "stderr": str(data.get("stderr") or "")[:2000] or None,
                "item_count": data.get("item_count"),
                "censored": bool(data.get("censored")),
                "delegated_to": data.get("delegated_to"),
                "ts": event.get("t"),
            }
            task["finished_at"] = event.get("t")

    def _ev_item_result(self, event: Dict[str, Any]) -> None:
        task = self.tasks.get(event.get("task_uuid", ""))
        if task is None:
            return
        items = task.setdefault("items", [])
        if len(items) < 500:
            data = event.get("data") or {}
            items.append({
                "host": event.get("host"),
                "status": event.get("status"),
                "item": data.get("item"),
                "msg": str(data.get("msg") or "")[:500] or None,
            })

    def _ev_diff(self, event: Dict[str, Any]) -> None:
        task = self.tasks.get(event.get("task_uuid", ""))
        if task is None:
            return
        diffs = task.setdefault("diffs", [])
        if len(diffs) < 50:
            diffs.append({"host": event.get("host"), "diff": event.get("diff")})

    def _ev_retry(self, event: Dict[str, Any]) -> None:
        host = self._host(event.get("host", "?"))
        host["state"] = "wiederholt"
        host["updated_at"] = event.get("t")

    def _ev_stats(self, event: Dict[str, Any]) -> None:
        self.stats = event.get("hosts") or {}
        for name, summary in self.stats.items():
            host = self._host(name)
            host["current_task"] = None
            host["current_task_name"] = ""
            if summary.get("unreachable"):
                host["state"] = "unreachable"
            elif summary.get("failures"):
                host["state"] = "failed"
            else:
                host["state"] = "fertig"

    def _ev_no_hosts_matched(self, event: Dict[str, Any]) -> None:
        self.notices.append("Kein Host passte auf das Muster dieses Plays.")

    def _ev_no_hosts_remaining(self, event: Dict[str, Any]) -> None:
        self.notices.append("Keine Hosts mehr uebrig - der Lauf wurde abgebrochen.")

    def _ev_include(self, event: Dict[str, Any]) -> None:
        pass

    def _ev_notify(self, event: Dict[str, Any]) -> None:
        pass

    def _ev_playbook_start(self, event: Dict[str, Any]) -> None:
        pass

    # -- Ausgabe ----------------------------------------------------------

    def progress(self, planned_total: Optional[int]) -> Dict[str, Any]:
        done = sum(1 for uuid_ in self.task_order if not self.tasks[uuid_]["running"]
                   and self.tasks[uuid_]["results"])
        total = max(planned_total or 0, len(self.task_order))
        return {"tasks_done": done, "tasks_seen": len(self.task_order), "tasks_total": total or None}

    def snapshot(self, planned_total: Optional[int] = None) -> Dict[str, Any]:
        return {
            "plays": self.plays,
            "tasks": self.tasks,
            "task_order": self.task_order,
            "hosts": self.hosts,
            "stats": self.stats,
            "notices": self.notices[-20:],
            "current_task": self.current_task,
            "truncated": self.truncated,
            "progress": self.progress(planned_total),
        }


class Run:
    """Ein einzelner Playbook-Lauf samt Artefakten."""

    def __init__(self, cfg: Config, run_id: str, request: RunRequest) -> None:
        self.cfg = cfg
        self.id = run_id
        self.request = request
        self.dir = cfg.runs_dir / run_id
        self.status = QUEUED
        self.rc: Optional[int] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.pid: Optional[int] = None
        self.argv: List[str] = []
        self.error: Optional[str] = None
        self.planned: Optional[Dict[str, Any]] = None
        self.cancel_requested = False
        self.state = RunState()
        self.log_tail: deque = deque(maxlen=LOG_TAIL_LINES)
        self.log_lines = 0
        self.log_bytes = 0
        self.log_truncated = False
        self._process: Optional[asyncio.subprocess.Process] = None
        self._log_handle = None
        self.loaded_from_disk = False

    # -- Pfade ------------------------------------------------------------

    @property
    def event_file(self) -> Path:
        return self.dir / "events.ndjson"

    @property
    def log_file(self) -> Path:
        return self.dir / "output.log"

    @property
    def meta_file(self) -> Path:
        return self.dir / "meta.json"

    @property
    def extra_vars_file(self) -> Path:
        return self.dir / "extra_vars.yaml"

    @property
    def duration(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 2)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES

    # -- Serialisierung ---------------------------------------------------

    def rc_meaning(self) -> Optional[str]:
        if self.rc is None:
            return None
        if self.rc < 0:
            name = signal.Signals(-self.rc).name if -self.rc in [s.value for s in signal.Signals] else str(-self.rc)
            return "Durch Signal %s beendet" % name
        return RC_MEANING.get(self.rc, "Rueckgabecode %d" % self.rc)

    def meta(self) -> Dict[str, Any]:
        planned_total = (self.planned or {}).get("task_count")
        counts = {"ok": 0, "changed": 0, "failed": 0, "skipped": 0, "unreachable": 0}
        for host in self.state.hosts.values():
            for key, value in host["counts"].items():
                counts[key] = counts.get(key, 0) + value
        failed_hosts = sorted(name for name, host in self.state.hosts.items()
                              if host["state"] in ("failed", "unreachable"))
        return {
            "id": self.id,
            "status": self.status,
            "rc": self.rc,
            "rc_meaning": self.rc_meaning(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "pid": self.pid,
            "error": self.error,
            "request": self.request.to_dict(),
            "argv": self.argv,
            "planned": self.planned,
            "host_count": len(self.state.hosts),
            "failed_hosts": failed_hosts,
            "counts": counts,
            "progress": self.state.progress(planned_total),
            "log_lines": self.log_lines,
            "log_truncated": self.log_truncated,
        }

    def write_meta(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.meta_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.meta(), indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(self.meta_file))
        except OSError:
            pass

    def snapshot(self) -> Dict[str, Any]:
        planned_total = (self.planned or {}).get("task_count")
        return {
            "meta": self.meta(),
            "state": self.state.snapshot(planned_total),
            "log_tail": list(self.log_tail),
        }

    # -- Wiederherstellung von der Platte --------------------------------

    @classmethod
    def from_disk(cls, cfg: Config, run_dir: Path) -> Optional["Run"]:
        try:
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        request_data = meta.get("request") or {}
        allowed = {f for f in RunRequest.__dataclass_fields__}
        request = RunRequest(**{k: v for k, v in request_data.items() if k in allowed})
        run = cls(cfg, meta.get("id") or run_dir.name, request)
        run.status = meta.get("status") or FAILED
        if run.status in ACTIVE_STATES:
            run.status = INTERRUPTED
            run.error = "Der Dienst wurde waehrend des Laufs beendet."
        run.rc = meta.get("rc")
        run.created_at = meta.get("created_at") or run.created_at
        run.started_at = meta.get("started_at")
        run.finished_at = meta.get("finished_at") or run.started_at
        run.argv = meta.get("argv") or []
        run.planned = meta.get("planned")
        run.error = meta.get("error") or run.error
        run.log_lines = meta.get("log_lines") or 0
        run.log_truncated = bool(meta.get("log_truncated"))
        run.loaded_from_disk = True
        return run

    def rebuild_state(self) -> None:
        """Baut den Zustand aus der Ereignisdatei neu auf (fuer alte Laeufe)."""
        if self.state.task_order or self.state.plays:
            return
        try:
            with self.event_file.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.state.apply(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return

    def load_log_tail(self, lines: int = LOG_TAIL_LINES) -> None:
        if self.log_tail:
            return
        try:
            with self.log_file.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                chunk = min(size, 1024 * 1024)
                handle.seek(size - chunk)
                data = handle.read().decode("utf-8", "replace")
        except OSError:
            return
        for line in data.splitlines()[-lines:]:
            self.log_tail.append(line)


class RunManager:
    """Warteschlange und Lebenszyklus aller Laeufe."""

    def __init__(self, cfg: Config, bus: Broadcaster, audit=None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.audit = audit
        self.runs: Dict[str, Run] = {}
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._workers: List[asyncio.Task] = []
        self._pending_log: Dict[str, List[str]] = {}
        self._pending_events: Dict[str, List[Dict[str, Any]]] = {}
        self._flusher: Optional[asyncio.Task] = None
        self._callback_settings: Optional[Tuple[List[str], List[str]]] = None
        self._callback_checked = 0.0

    # -- Start und Stop ---------------------------------------------------

    async def start(self) -> None:
        self.load_existing()
        for _ in range(self.cfg.max_concurrent):
            self._workers.append(asyncio.create_task(self._worker()))
        self._flusher = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        if self._flusher:
            self._flusher.cancel()
        for run in self.runs.values():
            if run.status == RUNNING and run._process is not None:
                await self._terminate(run, grace=2.0)

    def load_existing(self) -> None:
        if not self.cfg.runs_dir.is_dir():
            return
        for run_dir in sorted(self.cfg.runs_dir.iterdir()):
            if not run_dir.is_dir():
                continue
            run = Run.from_disk(self.cfg, run_dir)
            if run is None:
                continue
            self.runs[run.id] = run
            if run.status == INTERRUPTED:
                run.write_meta()

    # -- Oeffentliche API -------------------------------------------------

    def new_run_id(self) -> str:
        return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])

    def resolve_playbook(self, relative: str) -> Path:
        """Prueft, dass das Playbook im Ansible-Verzeichnis liegt."""
        raw = (relative or "").strip()
        if not raw:
            raise RunError("Kein Playbook angegeben")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.cfg.ansible_dir / candidate).resolve()
        try:
            resolved.relative_to(self.cfg.ansible_dir)
        except ValueError:
            raise RunError("Playbook liegt ausserhalb des Ansible-Verzeichnisses")
        if not resolved.is_file():
            raise RunError("Playbook nicht gefunden: %s" % raw)
        if resolved.suffix.lower() not in (".yml", ".yaml"):
            raise RunError("Playbooks muessen auf .yml oder .yaml enden")
        return resolved

    async def submit(self, request: RunRequest) -> Run:
        playbook = self.resolve_playbook(request.playbook)
        request.playbook = str(playbook.relative_to(self.cfg.ansible_dir))

        if request.extra_vars.strip():
            try:
                parsed = yaml.safe_load(request.extra_vars)
            except yaml.YAMLError as exc:
                raise RunError("Zusatzvariablen sind kein gueltiges YAML/JSON: %s" % exc)
            if parsed is not None and not isinstance(parsed, dict):
                raise RunError("Zusatzvariablen muessen ein Mapping (key: value) sein")

        run = Run(self.cfg, self.new_run_id(), request)
        run.dir.mkdir(parents=True, exist_ok=True)
        run.event_file.touch()
        run.log_file.touch()
        if request.extra_vars.strip():
            run.extra_vars_file.write_text(request.extra_vars, encoding="utf-8")
        run.write_meta()
        self.runs[run.id] = run
        await self._queue.put(run.id)
        self._publish_run_update(run)
        self._prune()
        return run

    def get(self, run_id: str) -> Optional[Run]:
        run = self.runs.get(run_id)
        if run is not None and not run.is_active:
            run.rebuild_state()
            run.load_log_tail()
        return run

    def list_runs(self, limit: int = 100, *, status: Optional[str] = None,
                  playbook: Optional[str] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
        items = sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)
        out: List[Dict[str, Any]] = []
        for run in items:
            if status and run.status != status:
                continue
            if playbook and run.request.playbook != playbook:
                continue
            if source and run.request.source != source:
                continue
            out.append(run.meta())
            if len(out) >= limit:
                break
        return out

    def active_runs(self) -> List[Run]:
        return [r for r in self.runs.values() if r.is_active]

    async def cancel(self, run_id: str, by: str = "-") -> bool:
        run = self.runs.get(run_id)
        if run is None or not run.is_active:
            return False
        run.cancel_requested = True
        run.error = "Abgebrochen von %s" % by
        if run.status == QUEUED:
            run.status = CANCELLED
            run.finished_at = time.time()
            run.write_meta()
            self._publish_run_update(run)
            self._publish(run, {"type": "status", "meta": run.meta()})
            return True
        await self._terminate(run)
        return True

    # -- Interna ----------------------------------------------------------

    async def _terminate(self, run: Run, grace: float = 8.0) -> None:
        """Bricht einen Lauf moeglichst sauber ab.

        Zuerst bekommt nur der Hauptprozess SIGINT - Ansible faengt das ab,
        beendet seine Worker selbst und schreibt ein verwertbares Protokoll.
        Erst wenn das nicht greift, wird die ganze Prozessgruppe beendet,
        damit keine verwaisten SSH- oder Kindprozesse zurueckbleiben.
        """
        process = run._process
        if process is None or process.returncode is not None:
            return
        self._append_log(run, "", notice="*** Abbruch angefordert ***")

        async def wait_for_exit(timeout: float) -> bool:
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False

        try:
            process.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            return
        if await wait_for_exit(grace):
            return

        for sig, wait in ((signal.SIGTERM, grace / 2), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.send_signal(sig)
                except (ProcessLookupError, OSError):
                    return
            if await wait_for_exit(wait):
                return

    async def _worker(self) -> None:
        while True:
            run_id = await self._queue.get()
            run = self.runs.get(run_id)
            try:
                if run is None or run.status != QUEUED:
                    continue
                await self._execute(run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                      # pragma: no cover
                if run is not None:
                    run.status = ERROR
                    run.error = "Interner Fehler: %s" % exc
                    run.finished_at = time.time()
                    run.write_meta()
                    self._publish_run_update(run)
            finally:
                self._queue.task_done()

    def _build_argv(self, run: Run) -> List[str]:
        req = run.request
        argv = [self.cfg.ansible_playbook, "-i", str(self.cfg.inventory), req.playbook]
        if req.limit.strip():
            argv += ["--limit", req.limit.strip()]
        if req.tags:
            argv += ["--tags", ",".join(req.tags)]
        if req.skip_tags:
            argv += ["--skip-tags", ",".join(req.skip_tags)]
        if req.check:
            argv.append("--check")
        if req.diff:
            argv.append("--diff")
        forks = req.forks or self.cfg.run_cfg.get("default_forks")
        if forks:
            argv += ["--forks", str(int(forks))]
        vault_file = self.cfg.run_cfg.get("vault_password_file")
        if vault_file:
            argv += ["--vault-password-file", str(vault_file)]
        if req.extra_vars.strip():
            argv += ["--extra-vars", "@%s" % run.extra_vars_file]
        if req.verbosity:
            argv.append("-" + "v" * min(4, int(req.verbosity)))
        return argv

    async def _callback_env_parts(self) -> Tuple[List[str], List[str]]:
        """Liest die bereits konfigurierten Callback-Pfade und -Namen aus Ansible.

        Damit ueberschreiben wir keine Callbacks, die der Administrator in
        ``ansible.cfg`` aktiviert hat, sondern ergaenzen unseren nur.
        """
        now = time.time()
        if self._callback_settings is not None and now - self._callback_checked < 300:
            return self._callback_settings
        paths: List[str] = []
        enabled: List[str] = []
        binary = shutil.which("ansible-config") or str(Path(self.cfg.ansible_playbook).with_name("ansible-config"))
        try:
            process = await asyncio.create_subprocess_exec(
                binary, "dump", cwd=str(self.cfg.ansible_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            for line in stdout.decode("utf-8", "replace").splitlines():
                match = re.match(r"^(DEFAULT_CALLBACK_PLUGIN_PATH|CALLBACKS_ENABLED|DEFAULT_CALLBACK_WHITELIST)"
                                 r"\([^)]*\)\s*=\s*(.*)$", line.strip())
                if not match:
                    continue
                try:
                    value = ast.literal_eval(match.group(2))
                except (ValueError, SyntaxError):
                    continue
                values = [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
                if match.group(1) == "DEFAULT_CALLBACK_PLUGIN_PATH":
                    paths = values
                else:
                    enabled = sorted(set(enabled) | set(values))
        except Exception:
            pass
        self._callback_settings = (paths, enabled)
        self._callback_checked = now
        return self._callback_settings

    async def _build_env(self, run: Run) -> Dict[str, str]:
        env = dict(os.environ)
        paths, enabled = await self._callback_env_parts()
        plugin_paths = [str(self.cfg.callback_dir)] + [p for p in paths if p != str(self.cfg.callback_dir)]
        enabled_names = sorted(set(enabled) | {"ansible_web_events"})
        env["ANSIBLE_CALLBACK_PLUGINS"] = os.pathsep.join(plugin_paths)
        env["ANSIBLE_CALLBACKS_ENABLED"] = ",".join(enabled_names)
        env["ANSIBLE_CALLBACK_WHITELIST"] = ",".join(enabled_names)
        env["ANSIBLE_WEB_EVENT_FILE"] = str(run.event_file)
        env["ANSIBLE_WEB_RUN_ID"] = run.id
        env["PYTHONUNBUFFERED"] = "1"
        if self.cfg.run_cfg.get("force_color"):
            env["ANSIBLE_FORCE_COLOR"] = "1"
        env.setdefault("ANSIBLE_NOCOWS", "1")
        for key, value in (self.cfg.run_cfg.get("extra_env") or {}).items():
            env[str(key)] = str(value)
        return env

    async def _list_tasks(self, run: Run, env: Dict[str, str]) -> None:
        """Ermittelt vorab die geplanten Tasks (fuer Fortschritt und Vorschau)."""
        argv = [a for a in self._build_argv(run) if a not in ("--diff", "--check")]
        argv.append("--list-tasks")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.cfg.ansible_dir), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
            )
            timeout = float(self.cfg.run_cfg.get("list_tasks_timeout") or 60)
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except Exception:
            return
        parsed = parse_list_tasks(stdout.decode("utf-8", "replace"))
        if parsed.get("task_count"):
            run.planned = parsed

    async def _execute(self, run: Run) -> None:
        if run.cancel_requested:
            return
        run.status = RUNNING
        run.started_at = time.time()
        env = await self._build_env(run)
        run.argv = self._build_argv(run)
        try:
            (run.dir / "command.txt").write_text(" ".join(run.argv) + "\n", encoding="utf-8")
        except OSError:
            pass
        await self._list_tasks(run, env)
        run.write_meta()
        self._publish_run_update(run)
        self._publish(run, {"type": "status", "meta": run.meta()})

        try:
            run._log_handle = run.log_file.open("ab")
        except OSError:
            run._log_handle = None

        self._append_log(run, "", notice="$ cd %s && %s" % (self.cfg.ansible_dir, " ".join(run.argv)))

        try:
            process = await asyncio.create_subprocess_exec(
                *run.argv,
                cwd=str(self.cfg.ansible_dir),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            run.status = ERROR
            run.error = "Start fehlgeschlagen: %s" % exc
            run.finished_at = time.time()
            self._close_log(run)
            run.write_meta()
            self._publish_run_update(run)
            self._publish(run, {"type": "status", "meta": run.meta()})
            return

        run._process = process
        run.pid = process.pid
        run.write_meta()
        if run.cancel_requested:
            await self._terminate(run)

        stop_events = asyncio.Event()
        tail_task = asyncio.create_task(self._pump_events(run, stop_events))
        try:
            await self._pump_stdout(run, process)
            run.rc = await process.wait()
        finally:
            stop_events.set()
            try:
                await asyncio.wait_for(tail_task, timeout=15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                tail_task.cancel()

        if run.cancel_requested:
            run.status = CANCELLED
        elif run.rc == 0:
            run.status = SUCCESS
        else:
            run.status = FAILED
        run.finished_at = time.time()
        run._process = None
        self._close_log(run)
        run.write_meta()
        self._flush_now(run)
        self._publish_run_update(run)
        self._publish(run, {"type": "status", "meta": run.meta(), "state": run.state.snapshot(
            (run.planned or {}).get("task_count"))})
        if self.audit is not None:
            self.audit.write(run.request.user, "run.finish", run.request.playbook,
                             ok=(run.status == SUCCESS),
                             detail={"run_id": run.id, "status": run.status, "rc": run.rc})

    async def _pump_stdout(self, run: Run, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        buffer = b""
        while True:
            try:
                chunk = await process.stdout.read(65536)
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not chunk:
                break
            buffer += chunk
            if b"\n" in buffer:
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    self._append_log(run, line.decode("utf-8", "replace").rstrip("\r"))
        if buffer:
            self._append_log(run, buffer.decode("utf-8", "replace").rstrip("\r"))

    async def _pump_events(self, run: Run, stop: asyncio.Event) -> None:
        handle = None
        buffer = ""
        try:
            while True:
                if handle is None:
                    try:
                        handle = run.event_file.open("r", encoding="utf-8", errors="replace")
                    except OSError:
                        handle = None
                data = handle.read() if handle is not None else ""
                if data:
                    buffer += data
                    *lines, buffer = buffer.split("\n")
                    batch: List[Dict[str, Any]] = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        run.state.apply(event)
                        batch.append(event)
                    if batch:
                        self._pending_events.setdefault(run.id, []).extend(batch)
                    continue
                if stop.is_set():
                    break
                await asyncio.sleep(0.12)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    # -- Logzeilen --------------------------------------------------------

    def _append_log(self, run: Run, line: str, notice: Optional[str] = None) -> None:
        text = notice if notice is not None else line
        run.log_lines += 1
        run.log_tail.append(text)
        self._pending_log.setdefault(run.id, []).append(text)
        if run._log_handle is not None:
            limit = int(self.cfg.run_cfg.get("max_log_bytes") or 0)
            payload = (text + "\n").encode("utf-8")
            if limit and run.log_bytes + len(payload) > limit:
                if not run.log_truncated:
                    run.log_truncated = True
                    try:
                        run._log_handle.write(b"\n*** Logdatei-Obergrenze erreicht, weitere Zeilen werden nicht gespeichert ***\n")
                        run._log_handle.flush()
                    except OSError:
                        pass
                return
            try:
                run._log_handle.write(payload)
                run.log_bytes += len(payload)
            except OSError:
                pass

    def _close_log(self, run: Run) -> None:
        if run._log_handle is not None:
            try:
                run._log_handle.flush()
                run._log_handle.close()
            except OSError:
                pass
            run._log_handle = None

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            for run_id in list(set(self._pending_log) | set(self._pending_events)):
                run = self.runs.get(run_id)
                if run is not None:
                    self._flush_now(run)

    def _flush_now(self, run: Run) -> None:
        lines = self._pending_log.pop(run.id, None)
        if lines:
            self._publish(run, {"type": "log", "lines": lines})
        events = self._pending_events.pop(run.id, None)
        if events:
            self._publish(run, {"type": "events", "events": events,
                                "progress": run.state.progress((run.planned or {}).get("task_count"))})

    # -- Verteilung -------------------------------------------------------

    def _publish(self, run: Run, message: Dict[str, Any]) -> None:
        self.bus.publish("run:%s" % run.id, message)

    def _publish_run_update(self, run: Run) -> None:
        self.bus.publish("runs", {"type": "run", "meta": run.meta()})

    # -- Aufraeumen -------------------------------------------------------

    def _prune(self) -> None:
        finished = sorted((r for r in self.runs.values() if not r.is_active),
                          key=lambda r: r.created_at, reverse=True)
        for run in finished[self.cfg.keep_runs:]:
            self.runs.pop(run.id, None)
            try:
                shutil.rmtree(run.dir, ignore_errors=True)
            except OSError:
                pass

    def delete(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if run is None or run.is_active:
            return False
        self.runs.pop(run_id, None)
        shutil.rmtree(run.dir, ignore_errors=True)
        self.bus.publish("runs", {"type": "run_deleted", "id": run_id})
        return True
