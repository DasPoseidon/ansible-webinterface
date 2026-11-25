"""Pruefung von Dateien vor dem Speichern.

Geprueft wird immer gegen eine temporaere Datei im Zielverzeichnis. Erst
wenn die Pruefung bestanden ist, wird die Originaldatei atomar ersetzt -
eine kaputte Konfiguration kann damit nie auf der Platte landen.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .config import Config


class ValidationResult:
    def __init__(self, ok: bool, kind: str, messages: Optional[List[str]] = None,
                 warnings: Optional[List[str]] = None) -> None:
        self.ok = ok
        self.kind = kind
        self.messages = messages or []
        self.warnings = warnings or []

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "kind": self.kind, "messages": self.messages,
                "warnings": self.warnings}


def _yaml_error_text(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    context = getattr(exc, "context", None)
    parts = []
    if mark is not None:
        parts.append("Zeile %d, Spalte %d" % (mark.line + 1, mark.column + 1))
    if context:
        parts.append(str(context))
    parts.append(str(problem))
    return ": ".join(parts)


class _DuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader, der doppelte Schluessel meldet statt sie still zu ueberschreiben."""

    duplicates: List[str]


def _construct_mapping(loader: "_DuplicateKeyLoader", node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        try:
            key = loader.construct_object(key_node, deep=deep)
        except yaml.YAMLError:
            continue
        try:
            hash(key)
        except TypeError:
            continue
        if key in mapping:
            loader.duplicates.append("Zeile %d: Schluessel '%s' ist mehrfach vorhanden"
                                     % (key_node.start_mark.line + 1, key))
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def check_yaml(text: str) -> ValidationResult:
    try:
        list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        return ValidationResult(False, "yaml", ["YAML-Fehler - %s" % _yaml_error_text(exc)])
    # Doppelte Schluessel sind gueltiges YAML, aber fast immer ein Versehen:
    # der letzte Wert gewinnt stillschweigend.
    duplicates: List[str] = []
    try:
        loader = _DuplicateKeyLoader(text)
        loader.duplicates = duplicates
        while loader.check_data():
            loader.get_data()
    except yaml.YAMLError:
        pass
    finally:
        try:
            loader.dispose()
        except Exception:
            pass
    return ValidationResult(True, "yaml", warnings=duplicates[:10])


def check_json(text: str) -> ValidationResult:
    try:
        json.loads(text)
    except ValueError as exc:
        return ValidationResult(False, "json", ["JSON-Fehler - %s" % exc])
    return ValidationResult(True, "json")


def check_ini(text: str) -> ValidationResult:
    parser = configparser.ConfigParser(strict=False, allow_no_value=True,
                                       interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        return ValidationResult(False, "ini", ["INI-Fehler - %s" % str(exc).replace("\n", " ")])
    return ValidationResult(True, "ini")


class Validator:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # -- Einstufung -------------------------------------------------------

    def classify(self, path: Path) -> str:
        """Bestimmt, wie eine Datei geprueft werden soll."""
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        try:
            inventory = self.cfg.inventory.resolve()
        except OSError:
            inventory = self.cfg.inventory
        if resolved == inventory:
            return "inventory"
        suffix = path.suffix.lower()
        if suffix in (".yml", ".yaml"):
            try:
                in_data = self.cfg.data_dir in resolved.parents
            except (OSError, ValueError):
                in_data = False
            if not in_data and self._looks_like_playbook(resolved):
                return "playbook"
            return "yaml"
        if suffix == ".json":
            return "json"
        if suffix in (".cfg", ".ini", ".conf") and path.name != "ansible.cfg":
            return "ini"
        if path.name == "ansible.cfg":
            return "ini"
        return "none"

    @staticmethod
    def _looks_like_playbook(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            return False
        if not isinstance(document, list) or not document:
            return False
        return any(isinstance(entry, dict) and ({"hosts", "import_playbook"} & set(entry))
                   for entry in document)

    # -- Pruefung ---------------------------------------------------------

    async def check_text(self, target: Path, text: str, kind: Optional[str] = None) -> ValidationResult:
        kind = kind or self.classify(target)

        pre_warnings: List[str] = []
        if kind in ("yaml", "playbook", "inventory"):
            basic = check_yaml(text)
            if not basic.ok:
                basic.kind = kind
                return basic
            pre_warnings = basic.warnings
        elif kind == "json":
            return check_json(text)
        elif kind == "ini":
            return check_ini(text)
        else:
            return ValidationResult(True, "none")

        if kind == "yaml":
            return ValidationResult(True, "yaml", warnings=pre_warnings)

        # Fuer Playbooks und Inventory zusaetzlich Ansible selbst fragen
        result = await self._check_with_ansible(target, text, kind)
        result.warnings = pre_warnings + result.warnings
        return result

    async def _check_with_ansible(self, target: Path, text: str, kind: str) -> ValidationResult:
        suffix = target.suffix or ".yml"
        tmp = target.parent / (".aw-pruefung-%s%s" % (secrets.token_hex(5), suffix))
        try:
            tmp.write_text(text, encoding="utf-8")
        except OSError as exc:
            return ValidationResult(True, kind, warnings=[
                "Konnte keine Pruefdatei anlegen (%s) - nur YAML wurde geprueft." % exc])
        try:
            if kind == "playbook":
                argv = [self.cfg.ansible_playbook, "-i", str(self.cfg.inventory),
                        "--syntax-check", str(tmp)]
            else:
                argv = [self.cfg.ansible_inventory_bin, "-i", str(tmp), "--list"]
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(self.cfg.ansible_dir),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
            except asyncio.TimeoutError:
                return ValidationResult(True, kind, warnings=[
                    "Ansible-Pruefung hat zu lange gebraucht und wurde abgebrochen."])
            except OSError as exc:
                return ValidationResult(True, kind, warnings=[
                    "Ansible-Pruefung nicht moeglich: %s" % exc])

            output = (stderr.decode("utf-8", "replace").strip() or
                      stdout.decode("utf-8", "replace").strip())
            output = output.replace(str(tmp), target.name)
            if process.returncode != 0:
                label = "Playbook-Syntaxpruefung" if kind == "playbook" else "Inventory-Pruefung"
                messages = [line for line in output.splitlines() if line.strip()][:12]
                return ValidationResult(False, kind, ["%s fehlgeschlagen:" % label] + messages)
            warnings = [line for line in output.splitlines()
                        if "WARNING" in line or "DEPRECAT" in line][:8]
            return ValidationResult(True, kind, warnings=[w.strip() for w in warnings])
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
