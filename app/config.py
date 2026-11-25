"""Konfiguration des Webinterfaces.

Die Datei ``config.yaml`` wird ausschliesslich gelesen, niemals von der
Anwendung zurueckgeschrieben - Kommentare des Administrators bleiben damit
erhalten. Alles was die Anwendung selbst verwaltet (Benutzer, Passwort-Hashes,
API-Tokens, Secret-Key) liegt unter ``state_dir`` in ``auth.json``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    """Fehlerhafte oder unvollstaendige Konfiguration."""


DEFAULTS: Dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8080,
        "secure_cookies": False,
        "session_ttl_hours": 12,
        "session_idle_hours": 8,
        "behind_proxy": False,
        "tls": {
            "enabled": False,
            "cert_file": None,
            "key_file": None,
            "self_signed": True,
            "hostnames": [],
            "common_name": None,
            "days": 825,
        },
    },
    "paths": {
        "ansible_dir": "/root/ansible",
        "inventory": "inventory.yaml",
        "data_dir": "files",
        "state_dir": "/root/ansible-web/state",
    },
    "discovery": {
        "playbook_globs": [
            "*.yml", "*.yaml",
            "playbooks/*.yml", "playbooks/*.yaml",
            "playbook/*.yml", "playbook/*.yaml",
        ],
        "exclude_dirs": [
            ".git", "roles", "group_vars", "host_vars", "collections",
            "vars", "defaults", "tasks", "handlers", "templates", "molecule",
            "__pycache__", ".ansible",
        ],
    },
    "run": {
        "ansible_playbook": None,
        "ansible_inventory": None,
        "max_concurrent": 1,
        "keep_runs": 300,
        "default_forks": None,
        "vault_password_file": None,
        "extra_env": {},
        "force_color": True,
        "max_log_bytes": 50 * 1024 * 1024,
        "list_tasks_timeout": 60,
    },
    "files": {
        "max_upload_bytes": 100 * 1024 * 1024,
        "max_edit_bytes": 2 * 1024 * 1024,
        "backups": True,
        "keep_backups": 30,
    },
    "git": {
        "enabled": True,
        "auto_commit": True,
        "push": False,
        "remote": "origin",
        "command": "git",
    },
    "cron": {
        "enabled": True,
        "crontab_command": "crontab",
        "runner_cli": None,
        "log_file": None,
        "api_url": None,
    },
    "ui": {
        "title": "Ansible Controller",
    },
}


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _deep_merge(base.get(key), value) if key in base else value
        return out
    return override


class Config:
    """Aufgeloeste Konfiguration mit absoluten Pfaden."""

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None) -> None:
        self.source = source
        self.raw: Dict[str, Any] = _deep_merge(DEFAULTS, data or {})

        paths = self.raw["paths"]
        self.ansible_dir = Path(os.path.expanduser(str(paths["ansible_dir"]))).resolve()
        self.state_dir = Path(os.path.expanduser(str(paths["state_dir"]))).resolve()
        self.inventory = self._resolve_under(self.ansible_dir, paths["inventory"])
        self.data_dir = self._resolve_under(self.ansible_dir, paths["data_dir"])

        tls = self.raw["server"].get("tls") or {}
        self.tls_enabled = bool(tls.get("enabled"))
        self.tls_dir = self.state_dir / "tls"
        self.tls_cert = (Path(os.path.expanduser(str(tls["cert_file"])))
                         if tls.get("cert_file") else self.tls_dir / "server.crt")
        self.tls_key = (Path(os.path.expanduser(str(tls["key_file"])))
                        if tls.get("key_file") else self.tls_dir / "server.key")

        self.runs_dir = self.state_dir / "runs"
        self.backup_dir = self.state_dir / "backups"
        self.auth_file = self.state_dir / "auth.json"
        self.sessions_file = self.state_dir / "sessions.json"
        self.audit_file = self.state_dir / "audit.ndjson"
        self.crontab_backup_dir = self.state_dir / "crontab-backups"

        pkg_root = Path(__file__).resolve().parent.parent
        self.package_dir = pkg_root
        self.static_dir = pkg_root / "static"
        self.callback_dir = pkg_root / "callback_plugins"

        run = self.raw["run"]
        self.ansible_playbook = run["ansible_playbook"] or shutil.which("ansible-playbook") or "ansible-playbook"
        self.ansible_inventory_bin = run["ansible_inventory"] or shutil.which("ansible-inventory") or "ansible-inventory"
        self.max_concurrent = max(1, int(run["max_concurrent"]))
        self.keep_runs = max(10, int(run["keep_runs"]))

        cron = self.raw["cron"]
        self.cron_log = Path(cron["log_file"]) if cron.get("log_file") else self.state_dir / "cron.log"
        self.cron_cli = Path(cron["runner_cli"]) if cron.get("runner_cli") else pkg_root / "bin" / "ansible-web-run"
        # Auf eine Adresse zeigen, die das Zertifikat auch abdeckt: lauscht der
        # Dienst auf allen Schnittstellen, spricht der Client ihn lokal an.
        listen_host = self.raw["server"]["host"]
        local_host = "127.0.0.1" if listen_host in ("0.0.0.0", "::", "") else listen_host
        self.cron_api_url = cron.get("api_url") or "%s://%s:%s" % (
            "https" if self.tls_enabled else "http", local_host, self.raw["server"]["port"])

    # -- Hilfsfunktionen ---------------------------------------------------

    @staticmethod
    def _resolve_under(base: Path, value: Any) -> Path:
        candidate = Path(os.path.expanduser(str(value)))
        if not candidate.is_absolute():
            candidate = base / candidate
        return Path(os.path.normpath(str(candidate)))

    def section(self, name: str) -> Dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def server(self) -> Dict[str, Any]:
        return self.raw["server"]

    @property
    def tls_cfg(self) -> Dict[str, Any]:
        return self.raw["server"].get("tls") or {}

    @property
    def use_secure_cookies(self) -> bool:
        """Mit TLS gehoert das Sitzungs-Cookie auf 'Secure', ohne waere es unerreichbar."""
        return bool(self.server.get("secure_cookies")) or self.tls_enabled

    @property
    def base_url(self) -> str:
        host = self.server["host"]
        shown = "localhost" if host in ("0.0.0.0", "::", "") else host
        return "%s://%s:%s" % ("https" if self.tls_enabled else "http", shown, self.server["port"])

    @property
    def run_cfg(self) -> Dict[str, Any]:
        return self.raw["run"]

    @property
    def files_cfg(self) -> Dict[str, Any]:
        return self.raw["files"]

    @property
    def git_cfg(self) -> Dict[str, Any]:
        return self.raw["git"]

    @property
    def cron_cfg(self) -> Dict[str, Any]:
        return self.raw["cron"]

    @property
    def discovery_cfg(self) -> Dict[str, Any]:
        return self.raw["discovery"]

    @property
    def editable_roots(self) -> List[Path]:
        """Verzeichnisse, in denen Dateioperationen erlaubt sind."""
        return [self.ansible_dir]

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.runs_dir, self.backup_dir, self.crontab_backup_dir):
            path.mkdir(parents=True, exist_ok=True)
        if self.tls_enabled:
            self.tls_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.tls_dir, 0o700)
            except OSError:
                pass
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

    def _subdirectories(self) -> List[str]:
        """Unterordner des Ansible-Verzeichnisses - als Vorschlag fuer data_dir."""
        skip = set(self.discovery_cfg.get("exclude_dirs") or []) | {"__pycache__"}
        try:
            names = sorted(entry.name for entry in self.ansible_dir.iterdir()
                           if entry.is_dir() and not entry.name.startswith(".")
                           and entry.name not in skip)
        except OSError:
            return []
        return names[:12]

    def validate(self) -> List[str]:
        """Gibt eine Liste von Problemen zurueck (leer = alles in Ordnung).

        Die Meldungen nennen moeglichst gleich die Loesung - wer sie liest,
        sitzt meist vor einer frischen Installation und soll nicht erst
        raten muessen, welcher Wert gemeint ist.
        """
        problems: List[str] = []
        if not self.ansible_dir.is_dir():
            problems.append(
                "Ansible-Verzeichnis nicht gefunden: %s - bitte paths.ansible_dir "
                "auf das Verzeichnis mit den Playbooks setzen." % self.ansible_dir)
            return problems

        if not self.inventory.exists():
            problems.append(
                "Inventory nicht gefunden: %s - bitte paths.inventory setzen "
                "(relativ zum Ansible-Verzeichnis oder absolut)." % self.inventory)

        if not self.data_dir.is_dir():
            hint = ""
            candidates = self._subdirectories()
            if candidates:
                hint = (" Vorhandene Unterordner, einer davon ist vermutlich gemeint: %s."
                        % ", ".join(candidates))
            elif self.ansible_dir.is_dir():
                hint = (" Im Ansible-Verzeichnis gibt es gar keinen Unterordner - dann "
                        "paths.data_dir auf \".\" setzen.")
            problems.append(
                "Daten-/Konfigordner nicht gefunden: %s - bitte paths.data_dir "
                "anpassen.%s" % (self.data_dir, hint))

        if not shutil.which(self.ansible_playbook) and not Path(self.ansible_playbook).exists():
            problems.append(
                "ansible-playbook nicht gefunden: %s - bitte run.ansible_playbook "
                "auf den vollstaendigen Pfad setzen (und run.ansible_inventory "
                "daneben). Als Dienst gestartet erbt die Anwendung nur einen "
                "knappen PATH, in dem z. B. /root/.local/bin oder ein venv fehlt; "
                "'command -v ansible-playbook' auf der Kommandozeile zeigt den Pfad."
                % self.ansible_playbook)
        return problems

    def to_public_dict(self) -> Dict[str, Any]:
        """Fuer die Oberflaeche sichtbare, unkritische Teile der Konfiguration."""
        return {
            "title": self.raw["ui"]["title"],
            "ansible_dir": str(self.ansible_dir),
            "inventory": str(self.inventory),
            "data_dir": str(self.data_dir),
            "data_dir_rel": self.relative(self.data_dir),
            "tls": self.tls_enabled,
            "git_enabled": bool(self.git_cfg["enabled"]),
            "cron_enabled": bool(self.cron_cfg["enabled"]),
            "max_concurrent": self.max_concurrent,
            "max_upload_bytes": int(self.files_cfg["max_upload_bytes"]),
            "max_edit_bytes": int(self.files_cfg["max_edit_bytes"]),
        }

    def relative(self, path: Path) -> str:
        """Pfad relativ zum Ansible-Verzeichnis, fuer die Anzeige."""
        try:
            return str(Path(path).resolve().relative_to(self.ansible_dir))
        except ValueError:
            return str(path)


def default_config_path() -> Path:
    env = os.environ.get("ANSIBLE_WEB_CONFIG")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent.parent / "config.yaml"
    if here.exists():
        return here
    return Path("/root/ansible-web/config.yaml")


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else default_config_path()
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError("config.yaml ist kein gueltiges YAML: %s" % exc) from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config.yaml muss ein YAML-Mapping enthalten")
        data = loaded
    cfg = Config(data, source=path if path.exists() else None)
    cfg.ensure_dirs()
    return cfg
