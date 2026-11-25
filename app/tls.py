"""HTTPS mit selbstsigniertem Zertifikat.

Der Dienst kann direkt verschluesselt ausliefern, ohne dass ein Reverse
Proxy davorstehen muss. Fehlt ein Zertifikat, wird eines erzeugt - mit
allen Namen und Adressen, unter denen der Controller erreichbar ist,
damit der Browser spaeter nicht zusaetzlich ueber einen unpassenden
Namen klagt.

Ein selbstsigniertes Zertifikat kennt kein Browser von sich aus. Beim
ersten Aufruf kommt also eine Warnung; wer sichergehen will, vergleicht
den Fingerabdruck, den der Dienst beim Start ausgibt und unter
Verwaltung anzeigt.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config

logger = logging.getLogger("ansible-web.tls")

RENEW_BEFORE_DAYS = 30


class TlsError(Exception):
    """Zertifikat konnte nicht erzeugt oder gelesen werden."""


# -- Namen und Adressen des Rechners sammeln -------------------------------


def local_names(extra: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    """Ermittelt DNS-Namen und IP-Adressen fuer das Zertifikat."""
    dns: List[str] = ["localhost"]
    ips: List[str] = ["127.0.0.1", "::1"]

    try:
        hostname = socket.gethostname()
        if hostname:
            dns.append(hostname)
            fqdn = socket.getfqdn()
            if fqdn and fqdn != hostname:
                dns.append(fqdn)
    except OSError:
        pass

    # Adressen aller Schnittstellen - ueber den Hostnamen ...
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.append(address)
    except (OSError, socket.gaierror):
        pass

    # ... und ueber die Adresse, mit der der Rechner nach aussen spraeche.
    # Es wird dabei nichts gesendet, der Socket wird nur zugeordnet.
    for family, probe in ((socket.AF_INET, "203.0.113.1"), (socket.AF_INET6, "2001:db8::1")):
        sock = None
        try:
            # Auf Rechnern ohne IPv6 schlaegt schon das Anlegen des Sockets fehl.
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.connect((probe, 1))
            ips.append(sock.getsockname()[0])
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()

    for entry in extra or []:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            ipaddress.ip_address(entry)
            ips.append(entry)
        except ValueError:
            dns.append(entry)

    def unique(values: List[str]) -> List[str]:
        seen, out = set(), []
        for value in values:
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    clean_ips = []
    for address in unique(ips):
        try:
            ipaddress.ip_address(address.split("%")[0])
            clean_ips.append(address.split("%")[0])
        except ValueError:
            continue
    return unique(dns), unique(clean_ips)


# -- Zertifikat erzeugen ----------------------------------------------------


def generate_self_signed(cert_file: Path, key_file: Path, *, dns: List[str],
                         ips: List[str], days: int = 825,
                         common_name: Optional[str] = None) -> None:
    """Erzeugt ein selbstsigniertes Zertifikat mit openssl."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise TlsError(
            "openssl wurde nicht gefunden. Entweder openssl installieren oder in der "
            "config.yaml unter server.tls ein vorhandenes Zertifikat eintragen.")

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.parent.mkdir(parents=True, exist_ok=True)

    alt_lines = []
    for index, name in enumerate(dns, start=1):
        alt_lines.append("DNS.%d = %s" % (index, name))
    for index, address in enumerate(ips, start=1):
        alt_lines.append("IP.%d = %s" % (index, address))

    config_text = "\n".join([
        "[req]",
        "distinguished_name = dn",
        "x509_extensions = v3",
        "req_extensions = v3",
        "prompt = no",
        "",
        "[dn]",
        "CN = %s" % (common_name or (dns[0] if dns else "ansible-web")),
        "O = Ansible-Webinterface",
        "",
        "[v3]",
        "basicConstraints = critical,CA:FALSE",
        "keyUsage = critical,digitalSignature,keyEncipherment",
        "extendedKeyUsage = serverAuth",
        "subjectAltName = @alt",
        "",
        "[alt]",
    ] + alt_lines) + "\n"

    tmp_cert = cert_file.with_suffix(cert_file.suffix + ".neu")
    tmp_key = key_file.with_suffix(key_file.suffix + ".neu")
    handle = tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False, encoding="utf-8")
    try:
        handle.write(config_text)
        handle.close()
        result = subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
             "-days", str(int(days)), "-keyout", str(tmp_key), "-out", str(tmp_cert),
             "-config", handle.name, "-extensions", "v3"],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise TlsError("openssl konnte kein Zertifikat erzeugen: %s"
                           % (result.stderr.strip() or result.stdout.strip())[:500])
        os.chmod(str(tmp_key), 0o600)
        os.chmod(str(tmp_cert), 0o644)
        os.replace(str(tmp_key), str(key_file))
        os.replace(str(tmp_cert), str(cert_file))
    except subprocess.TimeoutExpired:
        raise TlsError("openssl hat nicht innerhalb von 120 Sekunden geantwortet")
    except OSError as exc:
        raise TlsError("Zertifikat konnte nicht geschrieben werden: %s" % exc)
    finally:
        for leftover in (tmp_cert, tmp_key, Path(handle.name)):
            try:
                leftover.unlink()
            except OSError:
                pass


# -- Zertifikat lesen -------------------------------------------------------


def fingerprint(cert_file: Path) -> Optional[str]:
    """SHA-256-Fingerabdruck, so wie Browser ihn anzeigen."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def certificate_info(cert_file: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "file": str(cert_file),
        "exists": cert_file.is_file(),
        "fingerprint_sha256": None,
        "subject": None,
        "issuer": None,
        "not_after": None,
        "days_left": None,
        "sans": [],
        "self_signed": None,
    }
    if not info["exists"]:
        return info
    info["fingerprint_sha256"] = fingerprint(cert_file)

    openssl = shutil.which("openssl")
    if not openssl:
        return info
    try:
        result = subprocess.run(
            [openssl, "x509", "-in", str(cert_file), "-noout",
             "-subject", "-issuer", "-enddate", "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return info
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("subject="):
            info["subject"] = line[len("subject="):].strip()
        elif line.startswith("issuer="):
            info["issuer"] = line[len("issuer="):].strip()
        elif line.startswith("notAfter="):
            info["not_after"] = line[len("notAfter="):].strip()
        elif line.startswith(("DNS:", "IP Address:", "IP:")):
            info["sans"] = [part.strip() for part in line.split(",") if part.strip()]
    if info["subject"] and info["issuer"]:
        info["self_signed"] = info["subject"] == info["issuer"]
    if info["not_after"]:
        for fmt in ("%b %d %H:%M:%S %Y %Z", "%b %d %H:%M:%S %Y"):
            try:
                expires = time.mktime(time.strptime(info["not_after"].replace("GMT", "").strip(), fmt.replace(" %Z", "")))
                info["days_left"] = int((expires - time.time()) // 86400)
                break
            except ValueError:
                continue
    return info


def expires_within(cert_file: Path, days: int) -> bool:
    """True, wenn das Zertifikat in weniger als so vielen Tagen ablaeuft."""
    openssl = shutil.which("openssl")
    if openssl:
        try:
            result = subprocess.run(
                [openssl, "x509", "-in", str(cert_file), "-noout",
                 "-checkend", str(int(days) * 86400)],
                capture_output=True, text=True, timeout=30)
            return result.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    info = certificate_info(cert_file)
    if info["days_left"] is None:
        return False
    return info["days_left"] < days


# -- Einstiegspunkt ---------------------------------------------------------


def ensure_certificate(cfg: Config) -> Dict[str, Any]:
    """Sorgt dafuer, dass ein brauchbares Zertifikat bereitliegt.

    Gibt zurueck, was fuer uvicorn und die Anzeige gebraucht wird. Ist TLS
    abgeschaltet, passiert nichts.
    """
    tls = cfg.tls_cfg
    result: Dict[str, Any] = {
        "enabled": bool(tls.get("enabled")),
        "cert_file": str(cfg.tls_cert),
        "key_file": str(cfg.tls_key),
        "generated": False,
        "error": None,
    }
    if not result["enabled"]:
        return result

    cert_file, key_file = cfg.tls_cert, cfg.tls_key
    have_both = cert_file.is_file() and key_file.is_file()
    self_signed = bool(tls.get("self_signed", True))

    reason = None
    if not have_both:
        reason = "es liegt noch keines vor"
    elif self_signed and expires_within(cert_file, RENEW_BEFORE_DAYS):
        reason = "das vorhandene laeuft in weniger als %d Tagen ab" % RENEW_BEFORE_DAYS

    if reason:
        if not self_signed:
            result["error"] = (
                "Zertifikat oder Schluessel fehlt (%s / %s) und server.tls.self_signed "
                "ist abgeschaltet." % (cert_file, key_file))
            return result
        dns, ips = local_names(tls.get("hostnames") or [])
        logger.info("Erzeuge selbstsigniertes Zertifikat (%s)", reason)
        logger.info("  gueltig fuer: %s", ", ".join(dns + ips))
        try:
            try:
                default_cn = socket.gethostname() or "ansible-web"
            except OSError:
                default_cn = "ansible-web"
            generate_self_signed(cert_file, key_file, dns=dns, ips=ips,
                                 days=int(tls.get("days") or 825),
                                 common_name=tls.get("common_name") or default_cn)
            result["generated"] = True
        except TlsError as exc:
            result["error"] = str(exc)
            return result

    info = certificate_info(cert_file)
    result.update(info)
    if not key_file.is_file():
        result["error"] = "Der private Schluessel fehlt: %s" % key_file
    return result
