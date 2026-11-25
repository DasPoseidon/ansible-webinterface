"""Git-Anbindung fuer das Ansible-Verzeichnis.

Jede Aenderung ueber die Oberflaeche wird als eigener Commit festgehalten,
mit dem angemeldeten Benutzer als Autor. Historie, Diff und Rueckrollen
greifen auf dasselbe Repository zu, das auch auf der Kommandozeile liegt.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config

STATUS_LABELS = {
    "M": "geaendert",
    "A": "neu hinzugefuegt",
    "D": "geloescht",
    "R": "umbenannt",
    "C": "kopiert",
    "U": "Konflikt",
    "?": "unversioniert",
    "!": "ignoriert",
}


class GitError(Exception):
    """Ein Git-Befehl ist fehlgeschlagen."""


class GitRepo:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.ansible_dir
        self.binary = cfg.git_cfg.get("command") or "git"

    # -- Basis ------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.git_cfg.get("enabled")) and self.is_repo()

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def _author(self, user: str) -> Tuple[str, str]:
        name = (user or "ansible-web").strip() or "ansible-web"
        domain = self.cfg.git_cfg.get("author_domain") or "ansible-web.local"
        local = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-") or "web"
        return name, "%s@%s" % (local, domain)

    async def _run(self, args: Sequence[str], *, user: str = "", timeout: float = 60,
                   check: bool = True) -> Tuple[int, str, str]:
        argv = [self.binary, "-C", str(self.root), "-c", "safe.directory=%s" % self.root,
                "-c", "core.quotepath=false"]
        if user:
            name, email = self._author(user)
            argv += ["-c", "user.name=%s" % name, "-c", "user.email=%s" % email]
        argv += list(args)
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C.UTF-8"})
        if user:
            name, email = self._author(user)
            env.update({"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.root), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            raise GitError("Git-Befehl hat zu lange gebraucht: %s" % " ".join(args))
        except OSError as exc:
            raise GitError("Git konnte nicht gestartet werden: %s" % exc)
        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        rc = process.returncode or 0
        if check and rc != 0:
            raise GitError((err.strip() or out.strip() or "Git endete mit Code %d" % rc))
        return rc, out, err

    def _relative(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.root))

    # -- Zustand ----------------------------------------------------------

    async def info(self) -> Dict[str, Any]:
        if not self.is_repo():
            return {"repo": False, "enabled": False}
        data: Dict[str, Any] = {"repo": True, "enabled": bool(self.cfg.git_cfg.get("enabled"))}
        try:
            _, branch, _ = await self._run(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
            data["branch"] = branch.strip() or "(kein Commit)"
            _, head, _ = await self._run(["log", "-1", "--format=%h %an %ad %s", "--date=short"], check=False)
            data["head"] = head.strip()
            _, remotes, _ = await self._run(["remote"], check=False)
            data["remotes"] = [r for r in remotes.split() if r]
            _, counts, _ = await self._run(
                ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], check=False)
            parts = counts.split()
            if len(parts) == 2:
                data["ahead"], data["behind"] = int(parts[0]), int(parts[1])
        except GitError as exc:
            data["error"] = str(exc)
        data["changes"] = await self.status()
        data["dirty"] = len(data["changes"])
        return data

    async def status(self) -> List[Dict[str, Any]]:
        if not self.is_repo():
            return []
        try:
            _, out, _ = await self._run(["status", "--porcelain", "-z", "--untracked-files=normal"])
        except GitError:
            return []
        entries: List[Dict[str, Any]] = []
        tokens = [t for t in out.split("\0") if t]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if len(token) < 3:
                index += 1
                continue
            code, path = token[:2], token[3:]
            renamed_from = None
            if code[0] in ("R", "C") and index + 1 < len(tokens):
                renamed_from = tokens[index + 1]
                index += 1
            marker = code.strip()[:1] or "?"
            entries.append({
                "code": code,
                "path": path,
                "renamed_from": renamed_from,
                "label": STATUS_LABELS.get(marker, marker),
                "staged": code[0] not in (" ", "?"),
            })
            index += 1
        return entries

    # -- Schreiben --------------------------------------------------------

    async def commit_paths(self, paths: Sequence[Path], message: str, user: str,
                           *, allow_empty: bool = False) -> Dict[str, Any]:
        """Committet genau die angegebenen Pfade."""
        if not self.enabled:
            return {"committed": False, "reason": "Git ist deaktiviert oder kein Repository"}
        relatives: List[str] = []
        for path in paths:
            try:
                relatives.append(self._relative(path))
            except ValueError:
                continue
        if not relatives:
            return {"committed": False, "reason": "Keine Pfade im Repository"}
        try:
            await self._run(["add", "--all", "--"] + relatives, user=user)
            rc, out, _ = await self._run(["diff", "--cached", "--quiet", "--"] + relatives, check=False)
            if rc == 0 and not allow_empty:
                return {"committed": False, "reason": "Inhalt unveraendert - kein Commit noetig"}
            await self._run(["commit", "-m", message, "--"] + relatives, user=user)
            _, head, _ = await self._run(["rev-parse", "--short", "HEAD"], check=False)
        except GitError as exc:
            return {"committed": False, "reason": str(exc), "error": True}
        result = {"committed": True, "commit": head.strip(), "message": message, "paths": relatives}
        if self.cfg.git_cfg.get("push"):
            result["push"] = await self.push(user)
        return result

    async def push(self, user: str = "") -> Dict[str, Any]:
        remote = self.cfg.git_cfg.get("remote") or "origin"
        try:
            _, out, err = await self._run(["push", remote, "HEAD"], user=user, timeout=180)
        except GitError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "output": (err or out).strip()[:2000]}

    # -- Historie ---------------------------------------------------------

    async def log(self, path: Optional[Path] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.is_repo():
            return []
        args = ["log", "--max-count=%d" % max(1, min(limit, 500)),
                "--date=iso-strict", "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%s"]
        if path is not None:
            try:
                args += ["--follow", "--", self._relative(path)]
            except ValueError:
                return []
        try:
            _, out, _ = await self._run(args, check=False)
        except GitError:
            return []
        commits: List[Dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 5:
                commits.append({"hash": parts[0], "short": parts[1], "author": parts[2],
                                "date": parts[3], "subject": parts[4]})
        return commits

    async def diff_working(self, path: Optional[Path] = None) -> str:
        args = ["diff", "--no-color"]
        if path is not None:
            try:
                args += ["--", self._relative(path)]
            except ValueError:
                return ""
        try:
            _, out, _ = await self._run(args, check=False)
        except GitError as exc:
            return "Fehler: %s" % exc
        if not out.strip() and path is not None:
            # Datei ist vielleicht neu und noch unversioniert
            try:
                rc, _, _ = await self._run(["ls-files", "--error-unmatch", "--", self._relative(path)],
                                           check=False)
                if rc != 0:
                    return "Diese Datei ist noch nicht in Git erfasst (unversioniert)."
            except GitError:
                pass
        return out

    async def diff_commit(self, revision: str, path: Optional[Path] = None) -> str:
        args = ["show", "--no-color", "--format=%H%n%an <%ae>%n%ad%n%n%s%n%b%n", revision]
        if path is not None:
            try:
                args += ["--", self._relative(path)]
            except ValueError:
                return ""
        try:
            _, out, _ = await self._run(args, check=False)
        except GitError as exc:
            return "Fehler: %s" % exc
        return out

    async def file_at(self, path: Path, revision: str) -> str:
        """Inhalt einer Datei zu einem bestimmten Commit."""
        try:
            relative = self._relative(path)
        except ValueError:
            raise GitError("Datei liegt nicht im Repository")
        _, out, _ = await self._run(["show", "%s:%s" % (revision, relative)], timeout=60)
        return out
