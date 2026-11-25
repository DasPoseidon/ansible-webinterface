"""Verwaltung der Ansible-Cron-Jobs in der crontab von root.

Nur ein klar markierter Block wird von der Anwendung verwaltet:

    # >>> ansible-web verwaltet - nicht per Hand bearbeiten >>>
    ...
    # <<< ansible-web verwaltet <<<

Alles ausserhalb dieses Blocks bleibt unberuehrt und wird in der
Oberflaeche nur angezeigt. Die erzeugten Zeilen sind selbsterklaerend:
sie rufen den CLI-Client mit allen Optionen auf, sodass man auch ohne
die Anwendung sieht, was geplant ist.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shlex
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config

BEGIN_MARKER = "# >>> ansible-web verwaltet - nicht per Hand bearbeiten >>>"
END_MARKER = "# <<< ansible-web verwaltet <<<"
META_PREFIX = "# ansible-web:"

MACROS = {
    "@reboot": "beim Systemstart",
    "@yearly": "jaehrlich am 1. Januar um 00:00",
    "@annually": "jaehrlich am 1. Januar um 00:00",
    "@monthly": "monatlich am 1. um 00:00",
    "@weekly": "woechentlich sonntags um 00:00",
    "@daily": "taeglich um 00:00",
    "@midnight": "taeglich um 00:00",
    "@hourly": "stuendlich zur Minute 0",
}

# Optionen von ansible-playbook, die einen Wert tragen. Ihr Wert darf nicht
# versehentlich als Playbook-Pfad erkannt werden (z. B. "-i inventory.yaml").
VALUE_OPTIONS = {
    "-i", "--inventory", "--inventory-file", "-u", "--user", "--private-key",
    "--key-file", "-T", "--timeout", "-c", "--connection", "-M", "--module-path",
    "--vault-password-file", "--vault-id", "--become-user", "--become-method",
    "--start-at-task", "--ssh-common-args", "--ssh-extra-args", "--sftp-extra-args",
    "--scp-extra-args", "--module-name", "--new-vault-password-file", "--playbook-dir",
    "--flush-cache-file", "-l", "--limit", "--tags", "--skip-tags", "--forks", "-f",
    "--extra-vars", "-e", "--extra-vars-file", "--playbook", "-p", "--name",
}

# Ein Befehl gilt als Ansible-Aufruf, wenn eines seiner Argumente das
# Programm selbst ist. Reine Textsuche nach "ansible" waere falsch: ein
# Pfad wie /root/ansible-daten enthaelt das Wort, ruft aber nichts auf.
ANSIBLE_EXEC_RE = re.compile(r"(?:^|/)(ansible-playbook|ansible-web-run|ansible)$")


def _tokens(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def mentions_ansible(command: str) -> bool:
    return any(ANSIBLE_EXEC_RE.search(token) for token in _tokens(command))


FIELD_NAMES = ("Minute", "Stunde", "Tag des Monats", "Monat", "Wochentag")
FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAY_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


class CronError(Exception):
    """Fehler beim Lesen oder Schreiben der crontab."""


@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    schedule: str = "0 3 * * *"
    enabled: bool = True
    playbook: str = ""
    limit: str = ""
    tags: List[str] = field(default_factory=list)
    skip_tags: List[str] = field(default_factory=list)
    check: bool = False
    diff: bool = False
    extra_vars: str = ""
    forks: Optional[int] = None
    verbosity: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def validate_schedule(schedule: str) -> Tuple[bool, str]:
    """Prueft einen Cron-Ausdruck und gibt eine verstaendliche Fehlermeldung zurueck."""
    value = (schedule or "").strip()
    if not value:
        return False, "Kein Zeitplan angegeben"
    if value.startswith("@"):
        if value.lower() in MACROS:
            return True, ""
        return False, "Unbekanntes Kuerzel '%s'. Erlaubt: %s" % (
            value, ", ".join(sorted(MACROS)))
    fields = value.split()
    if len(fields) != 5:
        return False, ("Ein Zeitplan braucht genau 5 Felder "
                       "(Minute Stunde Tag Monat Wochentag), gefunden: %d" % len(fields))
    for index, raw in enumerate(fields):
        ok, error = _validate_field(raw, index)
        if not ok:
            return False, "Feld %d (%s): %s" % (index + 1, FIELD_NAMES[index], error)
    return True, ""


def _validate_field(raw: str, index: int) -> Tuple[bool, str]:
    low, high = FIELD_RANGES[index]
    for part in raw.split(","):
        if not part:
            return False, "leerer Abschnitt"
        step = None
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                return False, "Schrittweite '%s' ist keine positive Zahl" % step_text
            step = int(step_text)
            if step > (high - low + 1):
                return False, "Schrittweite %d ist groesser als der Wertebereich" % step
        if part in ("*", ""):
            continue
        bounds = part.split("-")
        if len(bounds) > 2:
            return False, "'%s' hat zu viele Bindestriche" % part
        for bound in bounds:
            value = _field_value(bound, index)
            if value is None:
                return False, "'%s' ist kein gueltiger Wert" % bound
            if not low <= value <= high:
                return False, "'%s' liegt nicht zwischen %d und %d" % (bound, low, high)
        if len(bounds) == 2:
            start, end = _field_value(bounds[0], index), _field_value(bounds[1], index)
            if start is not None and end is not None and start > end and index != 4:
                return False, "Bereich '%s' laeuft rueckwaerts" % part
    return True, ""


def _field_value(text: str, index: int) -> Optional[int]:
    text = (text or "").strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if index == 3 and text[:3] in MONTH_NAMES:
        return MONTH_NAMES.index(text[:3]) + 1
    if index == 4 and text[:3] in DAY_NAMES:
        return DAY_NAMES.index(text[:3])
    return None


def describe_schedule(schedule: str) -> str:
    """Kurzbeschreibung in Worten fuer die Oberflaeche."""
    value = (schedule or "").strip()
    if value.lower() in MACROS:
        return MACROS[value.lower()]
    fields = value.split()
    if len(fields) != 5:
        return value
    minute, hour, dom, month, dow = fields
    if minute.isdigit() and hour.isdigit() and (dom, month, dow) == ("*", "*", "*"):
        return "taeglich um %02d:%02d" % (int(hour), int(minute))
    if minute.isdigit() and hour.isdigit() and (dom, month) == ("*", "*"):
        names = {"0": "sonntags", "7": "sonntags", "1": "montags", "2": "dienstags",
                 "3": "mittwochs", "4": "donnerstags", "5": "freitags", "6": "samstags"}
        if dow in names:
            return "%s um %02d:%02d" % (names[dow], int(hour), int(minute))
        if dow == "1-5":
            return "montags bis freitags um %02d:%02d" % (int(hour), int(minute))
    if minute.isdigit() and hour.startswith("*/") and (dom, month, dow) == ("*", "*", "*"):
        return "alle %s Stunden zur Minute %s" % (hour[2:], int(minute))
    if minute.startswith("*/") and hour == "*":
        return "alle %s Minuten" % minute[2:]
    return value


def next_runs(schedule: str, count: int = 3) -> List[str]:
    value = (schedule or "").strip()
    if value.lower() == "@reboot":
        return []
    try:
        from croniter import croniter
    except ImportError:
        return []
    try:
        iterator = croniter(value, datetime.now())
        return [iterator.get_next(datetime).strftime("%Y-%m-%d %H:%M") for _ in range(count)]
    except Exception:
        return []


class CrontabService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.command = cfg.cron_cfg.get("crontab_command") or "crontab"
        self.vars_dir = cfg.state_dir / "cron-vars"

    # -- crontab lesen und schreiben --------------------------------------

    async def _crontab(self, args: List[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                self.command, *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode("utf-8") if stdin is not None else None),
                timeout=30)
        except asyncio.TimeoutError:
            raise CronError("crontab hat nicht geantwortet")
        except OSError as exc:
            raise CronError("crontab konnte nicht aufgerufen werden: %s "
                            "(ist cron im Container installiert?)" % exc)
        return (process.returncode or 0, stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"))

    async def read_raw(self) -> str:
        rc, stdout, stderr = await self._crontab(["-l"])
        if rc != 0:
            if "no crontab" in (stderr + stdout).lower():
                return ""
            raise CronError(stderr.strip() or "crontab -l endete mit Code %d" % rc)
        return stdout

    async def write_raw(self, content: str, *, user: str = "-") -> Dict[str, Any]:
        if not content.endswith("\n"):
            content += "\n"
        backup = await self._backup()
        rc, stdout, stderr = await self._crontab(["-"], stdin=content)
        if rc != 0:
            raise CronError(stderr.strip() or stdout.strip() or
                            "crontab konnte nicht geschrieben werden (Code %d)" % rc)
        return {"ok": True, "backup": backup}

    async def _backup(self) -> Optional[str]:
        try:
            current = await self.read_raw()
        except CronError:
            return None
        try:
            self.cfg.crontab_backup_dir.mkdir(parents=True, exist_ok=True)
            target = self.cfg.crontab_backup_dir / ("%s.crontab" % time.strftime("%Y%m%d-%H%M%S"))
            target.write_text(current, encoding="utf-8")
            os.chmod(str(target), 0o600)
            existing = sorted(self.cfg.crontab_backup_dir.glob("*.crontab"), reverse=True)
            for old in existing[40:]:
                old.unlink(missing_ok=True)
            return target.name
        except OSError:
            return None

    # -- Block zerlegen ---------------------------------------------------

    @staticmethod
    def _split(content: str) -> Tuple[List[str], List[str], List[str]]:
        lines = content.splitlines()
        try:
            start = lines.index(BEGIN_MARKER)
            end = lines.index(END_MARKER)
        except ValueError:
            return lines, [], []
        if end < start:
            return lines, [], []
        return lines[:start], lines[start + 1:end], lines[end + 1:]

    # -- Jobs lesen -------------------------------------------------------

    def _parse_meta(self, line: str) -> Dict[str, str]:
        body = line[len(META_PREFIX):].strip()
        meta: Dict[str, str] = {}
        try:
            for token in shlex.split(body):
                if "=" in token:
                    key, _, value = token.partition("=")
                    meta[key.strip()] = value
        except ValueError:
            pass
        return meta

    def _parse_job_line(self, line: str) -> Optional[Tuple[str, str]]:
        """Trennt Zeitplan und Befehl einer crontab-Zeile."""
        text = line.strip()
        disabled = False
        if text.startswith("#"):
            stripped = text.lstrip("#").strip()
            if not (stripped.startswith("@") or re.match(r"^[\d*]", stripped)):
                return None
            text = stripped
            disabled = True
        if text.startswith("@"):
            parts = text.split(None, 1)
            if len(parts) != 2:
                return None
            return parts[0], parts[1]
        fields = text.split(None, 5)
        if len(fields) < 6:
            return None
        return " ".join(fields[:5]), fields[5]

    def _job_from_command(self, schedule: str, command: str, meta: Dict[str, str],
                          enabled: bool) -> CronJob:
        job = CronJob(
            id=meta.get("id") or secrets.token_hex(4),
            name=meta.get("name") or "",
            schedule=schedule,
            enabled=enabled and meta.get("enabled", "true").lower() != "false",
        )
        command = command.replace("\\%", "%")
        # Umleitung am Ende abschneiden
        command = re.split(r"\s*(?:>>|>|2>&1|\|)\s*", command)[0]
        # Shell-Verkettungen wie "cd /pfad && ansible-playbook ..." aufloesen
        if "&&" in command or ";" in command:
            for segment in re.split(r"&&|;", command):
                if mentions_ansible(segment):
                    command = segment
                    break
        tokens = _tokens(command)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
            if token in ("--playbook", "-p"):
                job.playbook = nxt; index += 2; continue
            if token in ("--limit", "-l"):
                job.limit = nxt; index += 2; continue
            if token == "--tags":
                job.tags = [t for t in nxt.split(",") if t]; index += 2; continue
            if token == "--skip-tags":
                job.skip_tags = [t for t in nxt.split(",") if t]; index += 2; continue
            if token == "--forks":
                job.forks = int(nxt) if nxt.isdigit() else None; index += 2; continue
            if token == "--extra-vars-file":
                try:
                    job.extra_vars = Path(nxt).read_text(encoding="utf-8")
                except OSError:
                    job.extra_vars = ""
                index += 2; continue
            if token in ("--extra-vars", "-e"):
                job.extra_vars = nxt; index += 2; continue
            if token == "--name":
                job.name = job.name or nxt; index += 2; continue
            if token == "--check":
                job.check = True; index += 1; continue
            if token == "--diff":
                job.diff = True; index += 1; continue
            if re.match(r"^-v{1,4}$", token):
                job.verbosity = len(token) - 1; index += 1; continue
            # Sonstige Optionen mit Wert ueberspringen, damit ihr Wert nicht
            # als Playbook missverstanden wird
            if token in VALUE_OPTIONS:
                index += 2
                continue
            # Ansible direkt aufgerufen: Playbook ist das erste freie Argument
            if (not token.startswith("-") and not job.playbook
                    and token.endswith((".yml", ".yaml"))):
                job.playbook = token
            index += 1
        if not job.name:
            stem = Path(job.playbook).stem if job.playbook else ""
            job.name = stem.replace("-", " ").replace("_", " ").strip().capitalize() or "Cron-Job"
        return job

    async def list_jobs(self) -> Dict[str, Any]:
        content = await self.read_raw()
        before, managed, after = self._split(content)
        jobs: List[Dict[str, Any]] = []
        pending_meta: Dict[str, str] = {}
        for line in managed:
            text = line.strip()
            if not text:
                continue
            if text.startswith(META_PREFIX):
                pending_meta = self._parse_meta(text)
                continue
            parsed = self._parse_job_line(line)
            if parsed is None:
                continue
            schedule, command = parsed
            enabled = not line.strip().startswith("#")
            job = self._job_from_command(schedule, command, pending_meta, enabled)
            pending_meta = {}
            ok, error = validate_schedule(job.schedule)
            entry = job.to_dict()
            entry.update({
                "valid": ok,
                "schedule_error": error,
                "description": describe_schedule(job.schedule),
                "next_runs": next_runs(job.schedule) if job.enabled and ok else [],
                "command": command.strip(),
            })
            jobs.append(entry)

        others: List[Dict[str, Any]] = []
        for line in before + after:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            parsed = self._parse_job_line(line)
            if parsed is None:
                if re.match(r"^[A-Z_]+\s*=", text):
                    others.append({"raw": text, "kind": "setting", "ansible": False})
                continue
            schedule, command = parsed
            is_ansible = mentions_ansible(command)
            # Uebernehmbar ist eine Zeile nur, wenn daraus wirklich ein
            # Playbook erkennbar ist - sonst scheitert der Import nachher.
            detected = self._job_from_command(schedule, command, {}, True) if is_ansible else None
            others.append({
                "raw": text,
                "kind": "job",
                "schedule": schedule,
                "command": command.strip(),
                "ansible": is_ansible,
                "description": describe_schedule(schedule),
                "importable": bool(detected and detected.playbook),
                "detected_playbook": detected.playbook if detected else "",
            })
        return {
            "jobs": jobs,
            "unmanaged": others,
            "has_block": bool(managed) or BEGIN_MARKER in content,
            "raw": content,
            "cli": str(self.cfg.cron_cli),
            "log_file": str(self.cfg.cron_log),
        }

    # -- Jobs schreiben ---------------------------------------------------

    def _render_command(self, job: CronJob) -> str:
        parts = [shlex.quote(str(self.cfg.cron_cli)), "--playbook", shlex.quote(job.playbook)]
        if job.name:
            parts += ["--name", shlex.quote(job.name)]
        if job.limit.strip():
            parts += ["--limit", shlex.quote(job.limit.strip())]
        if job.tags:
            parts += ["--tags", shlex.quote(",".join(job.tags))]
        if job.skip_tags:
            parts += ["--skip-tags", shlex.quote(",".join(job.skip_tags))]
        if job.check:
            parts.append("--check")
        if job.diff:
            parts.append("--diff")
        if job.forks:
            parts += ["--forks", str(int(job.forks))]
        if job.verbosity:
            parts.append("-" + "v" * min(4, int(job.verbosity)))
        if job.extra_vars.strip():
            self.vars_dir.mkdir(parents=True, exist_ok=True)
            target = self.vars_dir / ("%s.yaml" % job.id)
            target.write_text(job.extra_vars, encoding="utf-8")
            os.chmod(str(target), 0o600)
            parts += ["--extra-vars-file", shlex.quote(str(target))]
        parts.append(">> %s 2>&1" % shlex.quote(str(self.cfg.cron_log)))
        # In crontab-Befehlen muss % maskiert werden
        return " ".join(parts).replace("%", "\\%")

    def _render_block(self, jobs: List[CronJob]) -> List[str]:
        lines = [BEGIN_MARKER,
                 "# Verwaltet vom Ansible-Webinterface. Aenderungen hier werden beim",
                 "# naechsten Speichern in der Oberflaeche ueberschrieben.",
                 ""]
        for job in jobs:
            meta = '%s id=%s name=%s enabled=%s' % (
                META_PREFIX, job.id, shlex.quote(job.name), "true" if job.enabled else "false")
            lines.append(meta)
            line = "%s %s" % (job.schedule, self._render_command(job))
            lines.append(line if job.enabled else "#" + line)
            lines.append("")
        lines.append(END_MARKER)
        return lines

    async def _current_jobs(self) -> List[CronJob]:
        data = await self.list_jobs()
        jobs: List[CronJob] = []
        allowed = set(CronJob.__dataclass_fields__)
        for entry in data["jobs"]:
            jobs.append(CronJob(**{k: v for k, v in entry.items() if k in allowed}))
        return jobs

    async def _save_jobs(self, jobs: List[CronJob], *, user: str) -> Dict[str, Any]:
        content = await self.read_raw()
        before, _, after = self._split(content)
        if BEGIN_MARKER not in content:
            before = [line for line in before]
            if before and before[-1].strip():
                before.append("")
        block = self._render_block(jobs) if jobs else []
        lines = before + block + after
        text = "\n".join(line.rstrip() for line in lines).strip("\n") + "\n"
        result = await self.write_raw(text, user=user)
        result["jobs"] = len(jobs)
        return result

    def _validate_job(self, job: CronJob) -> None:
        ok, error = validate_schedule(job.schedule)
        if not ok:
            raise CronError("Zeitplan ungueltig - %s" % error)
        if not job.playbook.strip():
            raise CronError("Es muss ein Playbook ausgewaehlt werden")
        candidate = (self.cfg.ansible_dir / job.playbook).resolve()
        try:
            candidate.relative_to(self.cfg.ansible_dir)
        except ValueError:
            raise CronError("Das Playbook liegt ausserhalb des Ansible-Verzeichnisses")
        if not candidate.is_file():
            raise CronError("Playbook nicht gefunden: %s" % job.playbook)
        if job.extra_vars.strip():
            import yaml
            try:
                parsed = yaml.safe_load(job.extra_vars)
            except yaml.YAMLError as exc:
                raise CronError("Zusatzvariablen sind kein gueltiges YAML: %s" % exc)
            if parsed is not None and not isinstance(parsed, dict):
                raise CronError("Zusatzvariablen muessen ein Mapping sein")

    async def create_job(self, job: CronJob, *, user: str) -> Dict[str, Any]:
        job.id = job.id or secrets.token_hex(4)
        self._validate_job(job)
        jobs = await self._current_jobs()
        if any(existing.id == job.id for existing in jobs):
            job.id = secrets.token_hex(4)
        jobs.append(job)
        result = await self._save_jobs(jobs, user=user)
        result["id"] = job.id
        return result

    async def update_job(self, job_id: str, job: CronJob, *, user: str) -> Dict[str, Any]:
        self._validate_job(job)
        jobs = await self._current_jobs()
        found = False
        for index, existing in enumerate(jobs):
            if existing.id == job_id:
                job.id = job_id
                jobs[index] = job
                found = True
                break
        if not found:
            raise CronError("Cron-Job nicht gefunden: %s" % job_id)
        return await self._save_jobs(jobs, user=user)

    async def delete_job(self, job_id: str, *, user: str) -> Dict[str, Any]:
        jobs = await self._current_jobs()
        remaining = [job for job in jobs if job.id != job_id]
        if len(remaining) == len(jobs):
            raise CronError("Cron-Job nicht gefunden: %s" % job_id)
        result = await self._save_jobs(remaining, user=user)
        (self.vars_dir / ("%s.yaml" % job_id)).unlink(missing_ok=True)
        return result

    async def set_enabled(self, job_id: str, enabled: bool, *, user: str) -> Dict[str, Any]:
        jobs = await self._current_jobs()
        for job in jobs:
            if job.id == job_id:
                job.enabled = bool(enabled)
                return await self._save_jobs(jobs, user=user)
        raise CronError("Cron-Job nicht gefunden: %s" % job_id)

    async def import_line(self, raw: str, *, user: str) -> Dict[str, Any]:
        """Uebernimmt eine bestehende, unverwaltete Zeile in den verwalteten Block."""
        parsed = self._parse_job_line(raw)
        if parsed is None:
            raise CronError("Die Zeile sieht nicht wie ein Cron-Eintrag aus")
        schedule, command = parsed
        job = self._job_from_command(schedule, command, {}, True)
        if not job.playbook:
            raise CronError("In der Zeile wurde kein Playbook erkannt. "
                            "Bitte den Job von Hand anlegen.")
        job.id = secrets.token_hex(4)
        self._validate_job(job)

        content = await self.read_raw()
        before, managed, after = self._split(content)
        target = raw.strip()
        before = [line for line in before if line.strip() != target]
        after = [line for line in after if line.strip() != target]
        jobs = [CronJob(**{k: v for k, v in entry.items()
                           if k in set(CronJob.__dataclass_fields__)})
                for entry in (await self.list_jobs())["jobs"]]
        jobs.append(job)
        block = self._render_block(jobs)
        text = "\n".join(line.rstrip() for line in (before + block + after)).strip("\n") + "\n"
        result = await self.write_raw(text, user=user)
        result["id"] = job.id
        return result

    async def list_backups(self) -> List[Dict[str, Any]]:
        try:
            items = sorted(self.cfg.crontab_backup_dir.glob("*.crontab"), reverse=True)
        except OSError:
            return []
        out = []
        for path in items[:40]:
            try:
                out.append({"name": path.name, "mtime": path.stat().st_mtime,
                            "size": path.stat().st_size})
            except OSError:
                continue
        return out

    async def read_backup(self, name: str) -> str:
        safe = Path(name).name
        path = self.cfg.crontab_backup_dir / safe
        if not path.is_file():
            raise CronError("Sicherungskopie nicht gefunden")
        return path.read_text(encoding="utf-8", errors="replace")
