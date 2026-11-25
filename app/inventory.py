"""Inventory einlesen - ueber ``ansible-inventory``, nicht per YAML-Parser.

Damit funktionieren auch dynamische Inventories, Inventory-Verzeichnisse,
Plugins und Variablen-Zusammenfuehrung genau so, wie Ansible sie selbst sieht.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .config import Config

MAX_VARS_BYTES = 2 * 1024 * 1024


class InventoryService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_key: Optional[float] = None
        self._lock = asyncio.Lock()

    # -- Cache ------------------------------------------------------------

    def _mtime_key(self) -> float:
        newest = 0.0
        candidates = [self.cfg.inventory, self.cfg.ansible_dir / "ansible.cfg"]
        if self.cfg.inventory.is_dir():
            try:
                candidates += [p for p in self.cfg.inventory.rglob("*") if p.is_file()]
            except OSError:
                pass
        for path in candidates:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return newest

    def invalidate(self) -> None:
        self._cache = None
        self._cache_key = None

    async def get(self, force: bool = False) -> Dict[str, Any]:
        async with self._lock:
            key = self._mtime_key()
            if not force and self._cache is not None and self._cache_key == key:
                return self._cache
            data = await self._load()
            self._cache = data
            self._cache_key = key
            return data

    # -- Laden ------------------------------------------------------------

    async def _load(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "source": str(self.cfg.inventory),
            "generated_at": time.time(),
            "groups": [],
            "hosts": [],
            "tree": [],
            "counts": {"hosts": 0, "groups": 0},
            "vars_omitted": False,
            "error": None,
            "warnings": [],
        }
        if not self.cfg.inventory.exists():
            result["error"] = "Inventory nicht gefunden: %s" % self.cfg.inventory
            return result

        argv = [self.cfg.ansible_inventory_bin, "-i", str(self.cfg.inventory), "--list"]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.cfg.ansible_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            result["error"] = "ansible-inventory hat nicht innerhalb von 90 Sekunden geantwortet"
            return result
        except OSError as exc:
            result["error"] = "ansible-inventory konnte nicht gestartet werden: %s" % exc
            return result

        stderr_text = stderr.decode("utf-8", "replace").strip()
        if process.returncode != 0:
            result["error"] = stderr_text or "ansible-inventory endete mit Code %s" % process.returncode
            return result
        if stderr_text:
            result["warnings"] = [line for line in stderr_text.splitlines() if line.strip()][:20]

        try:
            raw = json.loads(stdout.decode("utf-8", "replace") or "{}")
        except ValueError as exc:
            result["error"] = "Antwort von ansible-inventory war kein JSON: %s" % exc
            return result

        return self._shape(raw, result)

    # -- Aufbereitung -----------------------------------------------------

    @staticmethod
    def _shape(raw: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        meta = raw.get("_meta") or {}
        hostvars: Dict[str, Any] = meta.get("hostvars") or {}

        groups: Dict[str, Dict[str, Any]] = {}
        for name, body in raw.items():
            if name == "_meta" or not isinstance(body, dict):
                continue
            groups[name] = {
                "name": name,
                "hosts": sorted(body.get("hosts") or []),
                "children": sorted(body.get("children") or []),
                "vars": body.get("vars") or {},
            }

        # Elternbeziehungen und Host-Zugehoerigkeit auflösen
        parents: Dict[str, List[str]] = {name: [] for name in groups}
        for name, group in groups.items():
            for child in group["children"]:
                parents.setdefault(child, []).append(name)

        def all_hosts(group_name: str, seen: Optional[Set[str]] = None) -> Set[str]:
            seen = seen or set()
            if group_name in seen or group_name not in groups:
                return set()
            seen.add(group_name)
            found = set(groups[group_name]["hosts"])
            for child in groups[group_name]["children"]:
                found |= all_hosts(child, seen)
            return found

        host_groups: Dict[str, Set[str]] = {}
        for name in groups:
            for host in all_hosts(name):
                host_groups.setdefault(host, set()).add(name)

        host_names = sorted(set(hostvars) | set(host_groups))
        vars_size = 0
        hosts: List[Dict[str, Any]] = []
        for host in host_names:
            variables = hostvars.get(host) or {}
            try:
                vars_size += len(json.dumps(variables, default=str))
            except (TypeError, ValueError):
                pass
            hosts.append({
                "name": host,
                "groups": sorted(g for g in host_groups.get(host, set()) if g != "all"),
                "vars": variables,
            })
        if vars_size > MAX_VARS_BYTES:
            for host in hosts:
                host["vars"] = {}
            result["vars_omitted"] = True

        group_list: List[Dict[str, Any]] = []
        for name, group in sorted(groups.items()):
            if name == "all":
                continue
            members = sorted(all_hosts(name))
            group_list.append({
                "name": name,
                "hosts": members,
                "direct_hosts": group["hosts"],
                "children": group["children"],
                "parents": sorted(parents.get(name, [])),
                "host_count": len(members),
                "vars": group["vars"],
            })

        def build_tree(name: str, seen: Optional[Set[str]] = None) -> Optional[Dict[str, Any]]:
            seen = seen or set()
            if name in seen or name not in groups:
                return None
            seen = seen | {name}
            node = {
                "name": name,
                "hosts": groups[name]["hosts"],
                "host_count": len(all_hosts(name)),
                "children": [],
            }
            for child in groups[name]["children"]:
                sub = build_tree(child, seen)
                if sub is not None:
                    node["children"].append(sub)
            return node

        root = build_tree("all")
        result.update({
            "ok": True,
            "groups": group_list,
            "hosts": hosts,
            "tree": [root] if root else [],
            "counts": {"hosts": len(hosts), "groups": len(group_list)},
        })
        return result

    # -- Hilfen fuer die Oberflaeche --------------------------------------

    async def resolve_limit(self, limit: str) -> Dict[str, Any]:
        """Zeigt, welche Hosts ein --limit-Ausdruck tatsaechlich treffen wuerde."""
        limit = (limit or "").strip()
        argv = [self.cfg.ansible_inventory_bin, "-i", str(self.cfg.inventory), "--list"]
        if limit:
            argv += ["--limit", limit]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.cfg.ansible_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except (OSError, asyncio.TimeoutError) as exc:
            return {"ok": False, "error": str(exc), "hosts": []}
        if process.returncode != 0:
            return {"ok": False, "error": stderr.decode("utf-8", "replace").strip(), "hosts": []}
        try:
            raw = json.loads(stdout.decode("utf-8", "replace") or "{}")
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "hosts": []}
        hosts = sorted((raw.get("_meta") or {}).get("hostvars") or {})
        if not hosts:
            found: Set[str] = set()
            for name, body in raw.items():
                if name != "_meta" and isinstance(body, dict):
                    found |= set(body.get("hosts") or [])
            hosts = sorted(found)
        return {"ok": True, "error": None, "hosts": hosts, "count": len(hosts)}
