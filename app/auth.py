"""Benutzer, Rollen, Sitzungen und API-Tokens.

Alle Geheimnisse liegen ausschliesslich als Hash in ``state/auth.json``
(Dateirechte 0600). Sitzungen liegen getrennt in ``state/sessions.json``,
damit die Benutzerdatei bei normalem Betrieb unveraendert bleibt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PBKDF2_ITERATIONS = 210_000
SESSION_COOKIE = "aw_session"
CSRF_COOKIE = "aw_csrf"
CSRF_HEADER = "x-csrf-token"

ROLES: Tuple[str, ...] = ("viewer", "operator", "editor", "admin")
ROLE_LEVEL: Dict[str, int] = {name: index for index, name in enumerate(ROLES)}

ROLE_LABELS = {
    "viewer": "Betrachter - darf alles sehen, nichts aendern",
    "operator": "Operator - darf Playbooks starten und abbrechen",
    "editor": "Bearbeiter - darf zusaetzlich Dateien und Inventory aendern",
    "admin": "Administrator - darf zusaetzlich Cron, Benutzer und Audit verwalten",
}

# Faehigkeit -> benoetigte Mindestrolle
CAPABILITIES: Dict[str, str] = {
    "system.view": "viewer",
    "runs.view": "viewer",
    "runs.start": "operator",
    "runs.cancel": "operator",
    "inventory.view": "viewer",
    "files.read": "viewer",
    "files.write": "editor",
    "files.upload": "editor",
    "files.delete": "editor",
    "git.read": "viewer",
    "git.write": "editor",
    "cron.view": "viewer",
    "cron.write": "admin",
    "users.manage": "admin",
    "audit.view": "admin",
}


class AuthError(Exception):
    """Fehler bei Anmeldung oder Benutzerverwaltung."""


@dataclass
class Principal:
    """Wer gerade handelt - entweder eine Sitzung oder ein API-Token."""

    name: str
    role: str
    kind: str = "session"          # "session" | "token"
    token_id: Optional[str] = None
    session_token: Optional[str] = None
    csrf_token: Optional[str] = None

    def level(self) -> int:
        return ROLE_LEVEL.get(self.role, -1)

    def can(self, capability: str) -> bool:
        required = CAPABILITIES.get(capability)
        if required is None:
            return False
        return self.level() >= ROLE_LEVEL[required]

    def capabilities(self) -> Dict[str, bool]:
        return {name: self.can(name) for name in CAPABILITIES}

    def to_public(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "capabilities": self.capabilities(),
        }


# -- Passwort-Hashing ------------------------------------------------------


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password:
        raise AuthError("Passwort darf nicht leer sein")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_b64, hash_b64 = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


# -- Anmeldeversuche begrenzen --------------------------------------------


class LoginThrottle:
    """Einfache Bremse gegen Passwort-Raten, pro IP und pro Benutzername."""

    def __init__(self, threshold: int = 5, window: float = 900.0, base_delay: float = 30.0,
                 max_delay: float = 900.0) -> None:
        self.threshold = threshold
        self.window = window
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._failures: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _keys(self, ip: str, username: str) -> List[str]:
        return ["ip:%s" % (ip or "?"), "user:%s" % (username or "?").lower()]

    def retry_after(self, ip: str, username: str) -> float:
        now = time.time()
        worst = 0.0
        with self._lock:
            for key in self._keys(ip, username):
                events = [t for t in self._failures.get(key, []) if now - t < self.window]
                self._failures[key] = events
                if len(events) >= self.threshold:
                    over = len(events) - self.threshold
                    delay = min(self.base_delay * (2 ** over), self.max_delay)
                    remaining = delay - (now - events[-1])
                    worst = max(worst, remaining)
        return max(0.0, worst)

    def record_failure(self, ip: str, username: str) -> None:
        now = time.time()
        with self._lock:
            for key in self._keys(ip, username):
                events = [t for t in self._failures.get(key, []) if now - t < self.window]
                events.append(now)
                self._failures[key] = events[-50:]

    def record_success(self, ip: str, username: str) -> None:
        with self._lock:
            for key in self._keys(ip, username):
                self._failures.pop(key, None)


# -- Benutzer- und Sitzungsverwaltung -------------------------------------


class AuthStore:
    def __init__(self, auth_file: Path, sessions_file: Path, ttl_hours: float = 12.0,
                 idle_hours: float = 8.0) -> None:
        self.auth_file = Path(auth_file)
        self.sessions_file = Path(sessions_file)
        self.ttl = float(ttl_hours) * 3600.0
        self.idle = float(idle_hours) * 3600.0
        self._lock = threading.RLock()
        self.throttle = LoginThrottle()
        self._data: Dict[str, Any] = {"users": {}, "api_tokens": {}, "secret_key": ""}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._load()

    # -- Laden und Speichern ---------------------------------------------

    def _load(self) -> None:
        with self._lock:
            data = _read_json(self.auth_file, None)
            if isinstance(data, dict):
                self._data = {
                    "users": data.get("users") or {},
                    "api_tokens": data.get("api_tokens") or {},
                    "secret_key": data.get("secret_key") or "",
                }
            if not self._data.get("secret_key"):
                self._data["secret_key"] = secrets.token_urlsafe(48)
                self._save_auth()
            sessions = _read_json(self.sessions_file, {})
            self._sessions = sessions if isinstance(sessions, dict) else {}
            self._prune_sessions()

    def _save_auth(self) -> None:
        _write_private_json(self.auth_file, self._data)

    def _save_sessions(self) -> None:
        _write_private_json(self.sessions_file, self._sessions)

    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    # -- Benutzer ---------------------------------------------------------

    def has_users(self) -> bool:
        with self._lock:
            return bool(self._data["users"])

    def list_users(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for name, user in sorted(self._data["users"].items()):
                out.append({
                    "name": name,
                    "role": user.get("role", "viewer"),
                    "disabled": bool(user.get("disabled")),
                    "created_at": user.get("created_at"),
                    "last_login": user.get("last_login"),
                    "sessions": sum(1 for s in self._sessions.values() if s.get("user") == name),
                })
            return out

    @staticmethod
    def _check_username(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise AuthError("Benutzername darf nicht leer sein")
        if len(name) > 64:
            raise AuthError("Benutzername ist zu lang (max. 64 Zeichen)")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")
        if not set(name) <= allowed:
            raise AuthError("Benutzername erlaubt nur Buchstaben, Zahlen und . _ - @")
        return name

    @staticmethod
    def _check_role(role: str) -> str:
        if role not in ROLE_LEVEL:
            raise AuthError("Unbekannte Rolle: %s" % role)
        return role

    @staticmethod
    def check_password_quality(password: str) -> None:
        if len(password or "") < 10:
            raise AuthError("Passwort muss mindestens 10 Zeichen haben")

    def add_user(self, name: str, password: str, role: str = "viewer") -> Dict[str, Any]:
        name = self._check_username(name)
        role = self._check_role(role)
        self.check_password_quality(password)
        with self._lock:
            if name in self._data["users"]:
                raise AuthError("Benutzer existiert bereits: %s" % name)
            self._data["users"][name] = {
                "password_hash": hash_password(password),
                "role": role,
                "disabled": False,
                "created_at": time.time(),
                "last_login": None,
            }
            self._save_auth()
            return {"name": name, "role": role}

    def update_user(self, name: str, *, password: Optional[str] = None, role: Optional[str] = None,
                    disabled: Optional[bool] = None) -> None:
        with self._lock:
            user = self._data["users"].get(name)
            if not user:
                raise AuthError("Unbekannter Benutzer: %s" % name)
            if role is not None:
                role = self._check_role(role)
                if user.get("role") == "admin" and role != "admin" and self._count_active_admins() <= 1:
                    raise AuthError("Der letzte Administrator kann nicht herabgestuft werden")
                user["role"] = role
            if disabled is not None:
                if disabled and user.get("role") == "admin" and self._count_active_admins() <= 1:
                    raise AuthError("Der letzte Administrator kann nicht deaktiviert werden")
                user["disabled"] = bool(disabled)
                if disabled:
                    self._drop_user_sessions(name)
            if password is not None:
                self.check_password_quality(password)
                user["password_hash"] = hash_password(password)
                self._drop_user_sessions(name)
            self._save_auth()

    def delete_user(self, name: str) -> None:
        with self._lock:
            user = self._data["users"].get(name)
            if not user:
                raise AuthError("Unbekannter Benutzer: %s" % name)
            if user.get("role") == "admin" and self._count_active_admins() <= 1:
                raise AuthError("Der letzte Administrator kann nicht geloescht werden")
            del self._data["users"][name]
            self._drop_user_sessions(name)
            self._save_auth()

    def _count_active_admins(self) -> int:
        return sum(1 for u in self._data["users"].values()
                   if u.get("role") == "admin" and not u.get("disabled"))

    # -- Anmeldung --------------------------------------------------------

    def authenticate(self, name: str, password: str, ip: str = "") -> Principal:
        wait = self.throttle.retry_after(ip, name)
        if wait > 0:
            raise AuthError("Zu viele Fehlversuche. Bitte %d Sekunden warten." % int(wait + 0.5))
        with self._lock:
            user = self._data["users"].get((name or "").strip())
        ok = bool(user) and not user.get("disabled") and verify_password(password, user.get("password_hash", ""))
        if not ok:
            self.throttle.record_failure(ip, name)
            # Gleiche Meldung fuer alle Faelle, damit keine Benutzernamen verraten werden.
            raise AuthError("Benutzername oder Passwort ist falsch")
        self.throttle.record_success(ip, name)
        with self._lock:
            user["last_login"] = time.time()
            self._save_auth()
        return Principal(name=(name or "").strip(), role=user.get("role", "viewer"), kind="session")

    # -- Sitzungen --------------------------------------------------------

    def _prune_sessions(self) -> bool:
        now = time.time()
        changed = False
        for key, session in list(self._sessions.items()):
            created = float(session.get("created", 0))
            seen = float(session.get("last_seen", created))
            user = self._data["users"].get(session.get("user", ""))
            expired = (now - created > self.ttl) or (now - seen > self.idle)
            if expired or not user or user.get("disabled"):
                del self._sessions[key]
                changed = True
        return changed

    def _drop_user_sessions(self, name: str) -> None:
        for key, session in list(self._sessions.items()):
            if session.get("user") == name:
                del self._sessions[key]
        self._save_sessions()

    def create_session(self, principal: Principal, ip: str = "", user_agent: str = "") -> Principal:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._prune_sessions()
            self._sessions[_token_fingerprint(token)] = {
                "user": principal.name,
                "role": principal.role,
                "csrf": csrf,
                "created": now,
                "last_seen": now,
                "ip": ip,
                "user_agent": (user_agent or "")[:200],
            }
            self._save_sessions()
        principal.session_token = token
        principal.csrf_token = csrf
        return principal

    def resolve_session(self, token: Optional[str]) -> Optional[Principal]:
        if not token:
            return None
        key = _token_fingerprint(token)
        now = time.time()
        with self._lock:
            session = self._sessions.get(key)
            if not session:
                return None
            created = float(session.get("created", 0))
            seen = float(session.get("last_seen", created))
            if now - created > self.ttl or now - seen > self.idle:
                del self._sessions[key]
                self._save_sessions()
                return None
            user = self._data["users"].get(session.get("user", ""))
            if not user or user.get("disabled"):
                del self._sessions[key]
                self._save_sessions()
                return None
            # last_seen nur gelegentlich schreiben, um Schreiblast zu vermeiden
            if now - seen > 60:
                session["last_seen"] = now
                self._save_sessions()
            else:
                session["last_seen"] = now
            return Principal(
                name=session["user"],
                role=user.get("role", "viewer"),
                kind="session",
                session_token=token,
                csrf_token=session.get("csrf"),
            )

    def destroy_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._lock:
            if self._sessions.pop(_token_fingerprint(token), None) is not None:
                self._save_sessions()

    def destroy_all_sessions(self, user: Optional[str] = None) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if user is None or s.get("user") == user]
            for key in keys:
                del self._sessions[key]
            if keys:
                self._save_sessions()
            return len(keys)

    # -- API-Tokens (fuer den Cron-Client) -------------------------------

    def list_api_tokens(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{
                "id": token_id,
                "name": entry.get("name", token_id),
                "role": entry.get("role", "operator"),
                "created_at": entry.get("created_at"),
                "last_used": entry.get("last_used"),
            } for token_id, entry in sorted(self._data["api_tokens"].items())]

    def create_api_token(self, name: str, role: str = "operator") -> str:
        role = self._check_role(role)
        token_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(36)
        token = "awt_%s_%s" % (token_id, secret)
        with self._lock:
            self._data["api_tokens"][token_id] = {
                "name": (name or "token").strip()[:64],
                "role": role,
                "token_hash": _token_fingerprint(token),
                "created_at": time.time(),
                "last_used": None,
            }
            self._save_auth()
        return token

    def delete_api_token(self, token_id: str) -> None:
        with self._lock:
            if self._data["api_tokens"].pop(token_id, None) is None:
                raise AuthError("Unbekanntes Token: %s" % token_id)
            self._save_auth()

    def resolve_api_token(self, token: Optional[str]) -> Optional[Principal]:
        if not token or not token.startswith("awt_"):
            return None
        parts = token.split("_", 2)
        if len(parts) != 3:
            return None
        token_id = parts[1]
        fingerprint = _token_fingerprint(token)
        with self._lock:
            entry = self._data["api_tokens"].get(token_id)
            if not entry or not hmac.compare_digest(str(entry.get("token_hash", "")), fingerprint):
                return None
            entry["last_used"] = time.time()
            self._save_auth()
            return Principal(name="token:%s" % entry.get("name", token_id),
                             role=entry.get("role", "operator"),
                             kind="token", token_id=token_id)
