"""Dateiverwaltung fuer das Ansible-Verzeichnis.

Alle Pfade werden gegen das Ansible-Verzeichnis eingesperrt: aufgeloeste
Pfade muessen darunter liegen, Symlinks nach draussen werden abgewiesen,
und das ``.git``-Verzeichnis ist fuer Schreib- wie Lesezugriffe gesperrt.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat as stat_module
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from .config import Config
from .gitrepo import GitRepo
from .validate import Validator

SNIFF_BYTES = 8192
FORBIDDEN_NAMES = {".", ".."}
FORBIDDEN_TOP = {".git"}

EDITOR_MODES = {
    ".yml": "yaml", ".yaml": "yaml", ".json": "javascript", ".js": "javascript",
    ".py": "python", ".sh": "shell", ".bash": "shell", ".j2": "jinja2",
    ".cfg": "properties", ".ini": "properties", ".conf": "properties",
    ".properties": "properties", ".toml": "toml", ".env": "properties",
    ".dockerfile": "dockerfile", ".txt": None, ".md": None, ".csv": None,
}


class FileError(Exception):
    """Fehler bei einer Dateioperation (wird dem Benutzer gezeigt)."""


class AccessDenied(FileError):
    """Zugriff ausserhalb des erlaubten Bereichs."""


class Conflict(FileError):
    """Die Datei wurde zwischenzeitlich von jemand anderem geaendert."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_text_bytes(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        # Am Ende abgeschnittene Mehrbyte-Zeichen sind in Ordnung
        try:
            data[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def safe_component(name: str) -> str:
    """Prueft einen einzelnen Datei- oder Ordnernamen."""
    name = unicodedata.normalize("NFC", (name or "").strip())
    if not name or name in FORBIDDEN_NAMES:
        raise FileError("Ungueltiger Name")
    if "/" in name or "\\" in name or "\0" in name:
        raise FileError("Der Name darf keine Pfadtrenner enthalten")
    if name.startswith("-"):
        raise FileError("Der Name darf nicht mit einem Bindestrich beginnen")
    if len(name.encode("utf-8")) > 255:
        raise FileError("Der Name ist zu lang")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise FileError("Der Name enthaelt Steuerzeichen")
    return name


class FileService:
    def __init__(self, cfg: Config, validator: Validator, git: GitRepo) -> None:
        self.cfg = cfg
        self.validator = validator
        self.git = git
        self.root = cfg.ansible_dir

    # -- Pfade ------------------------------------------------------------

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        raw = (relative or "").strip()
        if "\0" in raw:
            raise AccessDenied("Ungueltiger Pfad")
        raw = raw.lstrip("/")
        candidate = (self.root / raw) if raw else self.root
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            raise AccessDenied("Pfad konnte nicht aufgeloest werden")
        if resolved != self.root and self.root not in resolved.parents:
            raise AccessDenied("Zugriff ausserhalb des Ansible-Verzeichnisses ist nicht erlaubt")
        try:
            parts = resolved.relative_to(self.root).parts
        except ValueError:
            parts = ()
        if parts and parts[0] in FORBIDDEN_TOP:
            raise AccessDenied("Das Verzeichnis .git kann hier nicht bearbeitet werden")
        if must_exist and not resolved.exists():
            raise FileError("Nicht gefunden: %s" % (raw or "/"))
        return resolved

    def rel(self, path: Path) -> str:
        try:
            value = str(Path(path).resolve().relative_to(self.root))
        except ValueError:
            return str(path)
        return "" if value == "." else value

    # -- Lesen ------------------------------------------------------------

    def _entry(self, path: Path) -> Dict[str, Any]:
        try:
            lstat = path.lstat()
        except OSError:
            return {"name": path.name, "type": "unknown", "error": "nicht lesbar"}
        is_link = stat_module.S_ISLNK(lstat.st_mode)
        target: Optional[str] = None
        broken = False
        outside = False
        if is_link:
            try:
                target = os.readlink(str(path))
                resolved = path.resolve()
                broken = not resolved.exists()
                outside = resolved != self.root and self.root not in resolved.parents
            except OSError:
                broken = True
        try:
            stat = path.stat()
            is_dir = stat_module.S_ISDIR(stat.st_mode)
            size = stat.st_size
            mtime = stat.st_mtime
            mode = stat_module.S_IMODE(stat.st_mode)
        except OSError:
            is_dir, size, mtime, mode = False, 0, lstat.st_mtime, 0

        suffix = path.suffix.lower()
        if outside:
            # Nichts ueber das Ziel ausserhalb des Jails verraten
            size, mtime, mode = 0, lstat.st_mtime, 0
        entry = {
            "name": path.name,
            "path": self.rel(path) if not outside else path.name,
            "type": "dir" if is_dir else "file",
            "size": None if is_dir else size,
            "mtime": mtime,
            "mode": "%04o" % mode,
            "symlink": is_link,
            "symlink_target": target,
            "broken": broken,
            "outside": outside,
            "editor_mode": EDITOR_MODES.get(suffix, None),
        }
        if not is_dir and not outside:
            entry["editable"] = (size <= int(self.cfg.files_cfg["max_edit_bytes"])
                                 and self._peek_is_text(path))
        else:
            entry["editable"] = False
        return entry

    def _peek_is_text(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return is_text_bytes(handle.read(SNIFF_BYTES))
        except OSError:
            return False

    def list_dir(self, relative: str) -> Dict[str, Any]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise FileError("Das ist kein Verzeichnis: %s" % self.rel(directory))
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise FileError("Verzeichnis nicht lesbar: %s" % exc)
        for child in children:
            try:
                if child.relative_to(self.root).parts[0] in FORBIDDEN_TOP:
                    continue
            except (ValueError, IndexError):
                pass
            entries.append(self._entry(child))
        current = self.rel(directory)
        parent = str(Path(current).parent) if current not in ("", ".") else None
        if parent in (".", ""):
            parent = "" if current else None
        return {
            "path": current,
            "parent": parent,
            "absolute": str(directory),
            "entries": entries,
            "count": len(entries),
            "is_data_dir": directory == self.cfg.data_dir,
        }

    def read_file(self, relative: str) -> Dict[str, Any]:
        path = self.resolve(relative)
        if path.is_dir():
            raise FileError("Das ist ein Verzeichnis")
        limit = int(self.cfg.files_cfg["max_edit_bytes"])
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise FileError("Datei nicht lesbar: %s" % exc)
        if size > limit:
            raise FileError("Datei ist zu gross fuer den Editor (%s, Grenze %s). "
                            "Sie kann heruntergeladen werden."
                            % (human_size(size), human_size(limit)))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise FileError("Datei nicht lesbar: %s" % exc)
        if not is_text_bytes(data):
            raise FileError("Das ist keine Textdatei - sie kann nur herunter- und hochgeladen werden.")
        text = data.decode("utf-8", "replace")
        return {
            "path": self.rel(path),
            "absolute": str(path),
            "content": text,
            "sha256": sha256_text(text),
            "size": size,
            "mtime": path.stat().st_mtime,
            "editor_mode": EDITOR_MODES.get(path.suffix.lower()),
            "kind": self.validator.classify(path),
            "backups": self.list_backups(path),
        }

    # -- Sicherungskopien -------------------------------------------------

    def _backup_dir(self, path: Path) -> Path:
        relative = self.rel(path) or path.name
        return self.cfg.backup_dir / relative

    def list_backups(self, path: Path) -> List[Dict[str, Any]]:
        directory = self._backup_dir(path)
        if not directory.is_dir():
            return []
        items: List[Dict[str, Any]] = []
        try:
            for child in sorted(directory.iterdir(), reverse=True):
                if child.is_file():
                    try:
                        items.append({"stamp": child.name, "size": child.stat().st_size,
                                      "mtime": child.stat().st_mtime})
                    except OSError:
                        continue
        except OSError:
            return []
        return items[:50]

    def _make_backup(self, path: Path) -> Optional[str]:
        if not self.cfg.files_cfg.get("backups") or not path.is_file():
            return None
        directory = self._backup_dir(path)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            target = directory / stamp
            counter = 1
            while target.exists():
                target = directory / ("%s-%d" % (stamp, counter))
                counter += 1
            shutil.copy2(str(path), str(target))
            keep = int(self.cfg.files_cfg.get("keep_backups") or 30)
            existing = sorted((p for p in directory.iterdir() if p.is_file()), reverse=True)
            for old in existing[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return target.name
        except OSError:
            return None

    def read_backup(self, relative: str, stamp: str) -> Dict[str, Any]:
        path = self.resolve(relative)
        name = safe_component(stamp)
        backup = self._backup_dir(path) / name
        if not backup.is_file():
            raise FileError("Sicherungskopie nicht gefunden")
        data = backup.read_bytes()
        if not is_text_bytes(data):
            raise FileError("Sicherungskopie ist keine Textdatei")
        return {"stamp": name, "content": data.decode("utf-8", "replace"),
                "mtime": backup.stat().st_mtime}

    # -- Schreiben --------------------------------------------------------

    def _atomic_write(self, path: Path, text: str) -> None:
        tmp = path.parent / (".aw-schreiben-%s.tmp" % os.urandom(5).hex())
        try:
            with tmp.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                try:
                    original = path.stat()
                    os.chmod(str(tmp), stat_module.S_IMODE(original.st_mode))
                    os.chown(str(tmp), original.st_uid, original.st_gid)
                except (OSError, AttributeError):
                    pass
            os.replace(str(tmp), str(path))
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise FileError("Schreiben fehlgeschlagen: %s" % exc)

    async def save_file(self, relative: str, content: str, *, user: str,
                        expected_sha: Optional[str] = None, message: str = "",
                        skip_validation: bool = False) -> Dict[str, Any]:
        path = self.resolve(relative, must_exist=False)
        if path.is_dir():
            raise FileError("Das ist ein Verzeichnis")
        existed = path.is_file()
        if not existed and not path.parent.is_dir():
            raise FileError("Das Zielverzeichnis existiert nicht")

        # Gleichzeitiges Bearbeiten erkennen
        if existed and expected_sha:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise FileError("Datei nicht lesbar: %s" % exc)
            current_sha = hashlib.sha256(current).hexdigest()
            text_sha = sha256_text(current.decode("utf-8", "replace"))
            if expected_sha not in (current_sha, text_sha):
                raise Conflict("Die Datei wurde zwischenzeitlich geaendert. "
                               "Bitte neu laden und die Aenderung erneut eintragen.")

        validation = {"ok": True, "kind": "none", "messages": [], "warnings": []}
        if not skip_validation:
            result = await self.validator.check_text(path, content)
            validation = result.to_dict()
            if not result.ok:
                return {"saved": False, "validation": validation}

        backup = self._make_backup(path) if existed else None
        self._atomic_write(path, content)

        git_result: Dict[str, Any] = {"committed": False}
        if self.git.enabled and self.cfg.git_cfg.get("auto_commit"):
            action = "aendert" if existed else "legt an"
            subject = message.strip() or "%s %s" % (action.capitalize(), self.rel(path))
            body = "Ueber das Ansible-Webinterface %s von %s." % (action, user)
            git_result = await self.git.commit_paths([path], "%s\n\n%s" % (subject, body), user)

        return {
            "saved": True,
            "path": self.rel(path),
            "created": not existed,
            "sha256": sha256_text(content),
            "size": len(content.encode("utf-8")),
            "backup": backup,
            "validation": validation,
            "git": git_result,
        }

    async def create_dir(self, relative: str, name: str, *, user: str) -> Dict[str, Any]:
        parent = self.resolve(relative)
        if not parent.is_dir():
            raise FileError("Das Ziel ist kein Verzeichnis")
        target = parent / safe_component(name)
        if target.exists():
            raise FileError("Es gibt dort schon einen Eintrag mit diesem Namen")
        try:
            target.mkdir(parents=False)
        except OSError as exc:
            raise FileError("Ordner konnte nicht angelegt werden: %s" % exc)
        return {"path": self.rel(target)}

    async def rename(self, relative: str, new_name: str, *, user: str) -> Dict[str, Any]:
        path = self.resolve(relative)
        if path == self.root:
            raise FileError("Das Ansible-Verzeichnis selbst kann nicht umbenannt werden")
        target = path.parent / safe_component(new_name)
        if target.exists():
            raise FileError("Es gibt dort schon einen Eintrag mit diesem Namen")
        try:
            os.rename(str(path), str(target))
        except OSError as exc:
            raise FileError("Umbenennen fehlgeschlagen: %s" % exc)
        git_result: Dict[str, Any] = {"committed": False}
        if self.git.enabled and self.cfg.git_cfg.get("auto_commit"):
            git_result = await self.git.commit_paths(
                [path, target],
                "Benennt %s zu %s um\n\nUeber das Ansible-Webinterface von %s."
                % (self.rel(path), self.rel(target), user), user)
        return {"path": self.rel(target), "git": git_result}

    async def delete(self, relative: str, *, user: str, recursive: bool = False) -> Dict[str, Any]:
        path = self.resolve(relative)
        if path == self.root:
            raise FileError("Das Ansible-Verzeichnis selbst kann nicht geloescht werden")
        if path == self.cfg.inventory:
            raise FileError("Das Inventory kann hier nicht geloescht werden")
        if path == self.cfg.data_dir:
            raise FileError("Der Daten-/Konfigordner kann hier nicht geloescht werden")
        is_dir = path.is_dir() and not path.is_symlink()
        if is_dir:
            try:
                has_children = any(path.iterdir())
            except OSError:
                has_children = True
            if has_children and not recursive:
                raise FileError("Der Ordner ist nicht leer. Loeschen muss ausdruecklich "
                                "bestaetigt werden.")
            self._make_backup_tree(path)
            try:
                shutil.rmtree(str(path))
            except OSError as exc:
                raise FileError("Loeschen fehlgeschlagen: %s" % exc)
        else:
            self._make_backup(path)
            try:
                path.unlink()
            except OSError as exc:
                raise FileError("Loeschen fehlgeschlagen: %s" % exc)
        git_result: Dict[str, Any] = {"committed": False}
        if self.git.enabled and self.cfg.git_cfg.get("auto_commit"):
            git_result = await self.git.commit_paths(
                [path], "Loescht %s\n\nUeber das Ansible-Webinterface von %s."
                % (self.rel(path), user), user)
        return {"path": self.rel(path), "git": git_result}

    def _make_backup_tree(self, directory: Path) -> None:
        """Sichert einen ganzen Ordner vor dem Loeschen als ZIP."""
        try:
            target_dir = self._backup_dir(directory)
            target_dir.mkdir(parents=True, exist_ok=True)
            archive = target_dir / ("%s-ordner.zip" % time.strftime("%Y%m%d-%H%M%S"))
            with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as zf:
                for item in directory.rglob("*"):
                    if item.is_file() and not item.is_symlink():
                        zf.write(str(item), str(item.relative_to(directory)))
        except (OSError, zipfile.BadZipFile, ValueError):
            pass

    # -- Hochladen --------------------------------------------------------

    async def upload(self, relative: str, filename: str, source: BinaryIO, *,
                     user: str, overwrite: bool = False) -> Dict[str, Any]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise FileError("Das Ziel ist kein Verzeichnis")
        name = safe_component(Path(filename or "").name)
        target = directory / name
        existed = target.exists()
        if existed and not overwrite:
            raise FileError("Es gibt dort schon eine Datei '%s'. Ueberschreiben muss "
                            "ausdruecklich bestaetigt werden." % name)
        if existed and target.is_dir():
            raise FileError("Dort liegt ein Ordner mit diesem Namen")

        limit = int(self.cfg.files_cfg["max_upload_bytes"])
        tmp = directory / (".aw-upload-%s.tmp" % os.urandom(5).hex())
        written = 0

        def copy_stream() -> int:
            total = 0
            with tmp.open("wb") as handle:
                while True:
                    chunk = source.read(1024 * 256)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise FileError("Datei ist groesser als das Limit von %s"
                                        % human_size(limit))
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            return total

        try:
            written = await asyncio.to_thread(copy_stream)
            if existed:
                self._make_backup(target)
                try:
                    original = target.stat()
                    os.chmod(str(tmp), stat_module.S_IMODE(original.st_mode))
                except OSError:
                    pass
            else:
                os.chmod(str(tmp), 0o644)
            os.replace(str(tmp), str(target))
        except FileError:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise FileError("Hochladen fehlgeschlagen: %s" % exc)

        git_result: Dict[str, Any] = {"committed": False}
        if self.git.enabled and self.cfg.git_cfg.get("auto_commit"):
            action = "ersetzt" if existed else "laedt"
            git_result = await self.git.commit_paths(
                [target], "%s %s hoch\n\nUeber das Ansible-Webinterface von %s."
                % (action.capitalize(), self.rel(target), user), user)
        return {"path": self.rel(target), "size": written, "replaced": existed,
                "git": git_result}

    # -- Herunterladen ----------------------------------------------------

    def download_file(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.is_file():
            raise FileError("Das ist keine Datei")
        return path

    def zip_directory(self, relative: str, *, max_bytes: int = 1024 * 1024 * 1024) -> Tuple[Path, str]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise FileError("Das ist kein Verzeichnis")
        label = directory.name or "ansible"
        temp_dir = self.cfg.state_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        archive = temp_dir / ("%s-%s.zip" % (label, os.urandom(4).hex()))
        total = 0
        try:
            with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as zf:
                for item in sorted(directory.rglob("*")):
                    if item.is_symlink() or not item.is_file():
                        continue
                    try:
                        if item.relative_to(self.root).parts[0] in FORBIDDEN_TOP:
                            continue
                        total += item.stat().st_size
                    except (OSError, ValueError, IndexError):
                        continue
                    if total > max_bytes:
                        raise FileError("Der Ordner ist zu gross fuer einen ZIP-Download (> %s)"
                                        % human_size(max_bytes))
                    zf.write(str(item), str(Path(label) / item.relative_to(directory)))
        except FileError:
            archive.unlink(missing_ok=True)
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            archive.unlink(missing_ok=True)
            raise FileError("ZIP-Archiv konnte nicht erstellt werden: %s" % exc)
        return archive, "%s.zip" % label


def human_size(value: Optional[float]) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024
    return "%.1f TB" % size
