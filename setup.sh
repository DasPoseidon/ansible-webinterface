#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Einrichtung des Ansible-Webinterfaces.
#
#   ./setup.sh                    venv anlegen, Abhaengigkeiten installieren,
#                                 Konfiguration vorbereiten
#   ./setup.sh --admin anna       zusaetzlich einen Administrator anlegen
#   ./setup.sh --tls              HTTPS einschalten (selbstsigniertes Zertifikat)
#   ./setup.sh --host 0.0.0.0     im ganzen Netz erreichbar statt nur lokal
#   ./setup.sh --no-venv          vorhandenes venv weiterverwenden
#                                 (der uebliche Aufruf nach einem git pull)
#
# Das Skript ist wiederholbar: ein erneuter Aufruf aktualisiert nur, was
# noetig ist, und ueberschreibt niemals eine vorhandene config.yaml.
# ---------------------------------------------------------------------------
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BASE/venv"
CONFIG="$BASE/config.yaml"
ADMIN=""
MAKE_VENV=1
USE_TLS=0
BIND_HOST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --admin)   ADMIN="${2:-}"; shift 2 ;;
        --host)    BIND_HOST="${2:-}"; shift 2 ;;
        --tls)     USE_TLS=1; shift ;;
        --no-venv) MAKE_VENV=0; shift ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mFehler:\033[0m %s\n' "$1" >&2; exit 1; }

# -- Voraussetzungen --------------------------------------------------------

say "Voraussetzungen pruefen"

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 wurde nicht gefunden."
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 oder neuer wird benoetigt, gefunden: $PYVER"
ok "Python $PYVER ($PYTHON)"

"$PYTHON" -c 'import venv' 2>/dev/null \
    || die "Das Modul venv fehlt. Unter Debian/Ubuntu: apt-get install python3-venv"

if command -v ansible-playbook >/dev/null 2>&1; then
    ok "$(ansible-playbook --version 2>/dev/null | head -1)"
else
    warn "ansible-playbook ist nicht im PATH. Pfad spaeter in der config.yaml unter run.ansible_playbook eintragen."
fi

if command -v crontab >/dev/null 2>&1; then
    ok "crontab vorhanden"
else
    warn "crontab fehlt - geplante Jobs lassen sich erst verwalten, wenn cron installiert ist."
fi

if command -v git >/dev/null 2>&1; then
    ok "git vorhanden"
else
    warn "git fehlt - Aenderungen werden dann nur als Sicherungskopie festgehalten."
fi

[ "$(id -u)" -eq 0 ] || warn "Nicht als root. Cron-Jobs von root und Dateien unter /root sind dann moeglicherweise nicht erreichbar."

# -- venv -------------------------------------------------------------------

say "Virtuelle Umgebung"
if [ -x "$VENV/bin/python" ]; then
    ok "venv vorhanden: $VENV"
elif [ "$MAKE_VENV" -eq 1 ]; then
    "$PYTHON" -m venv "$VENV"
    ok "venv angelegt: $VENV"
else
    die "--no-venv angegeben, aber unter $VENV liegt kein venv."
fi

# Die Abhaengigkeiten werden immer abgeglichen, auch mit --no-venv: beim
# Aktualisieren ist genau das der Zweck des Aufrufs.
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
say "Abhaengigkeiten abgleichen"
"$VENV/bin/pip" install --quiet -r "$BASE/requirements.txt" \
    || die "Die Abhaengigkeiten liessen sich nicht installieren. Besteht eine Netzverbindung?"
ok "$(grep -c '^[a-zA-Z]' "$BASE/requirements.txt") Pakete auf dem Stand aus requirements.txt"

# -- Konfiguration ----------------------------------------------------------

say "Konfiguration"

if [ -f "$CONFIG" ]; then
    ok "config.yaml ist vorhanden - sie wird nicht angefasst."
else
    cp "$BASE/config.example.yaml" "$CONFIG"
    chmod 600 "$CONFIG"
    ok "config.yaml aus der Vorlage angelegt"

    # Zustandsverzeichnis an den tatsaechlichen Installationsort anpassen -
    # die Vorlage geht von /root/ansible-web aus.
    "$VENV/bin/python" - "$CONFIG" "$BASE/state" <<'PY'
import re, sys
config, state = sys.argv[1], sys.argv[2]
text = open(config, encoding="utf-8").read()
text = re.sub(r"(?m)^(  state_dir:).*$", r"\1 " + state, text, count=1)
open(config, "w", encoding="utf-8").write(text)
PY
    ok "Zustandsverzeichnis: $BASE/state"

    # Ansible-Verzeichnis zu erraten versuchen: ein Verzeichnis mit
    # Inventory oder ansible.cfg, in der Naehe des ueblichen Orts.
    FOUND=""
    for CANDIDATE in /root/ansible /root/ansible-playbooks /root/playbooks "$(dirname "$BASE")"/ansible; do
        [ -d "$CANDIDATE" ] || continue
        if [ -f "$CANDIDATE/ansible.cfg" ] || [ -f "$CANDIDATE/inventory.yaml" ] \
           || [ -f "$CANDIDATE/inventory.yml" ] || [ -f "$CANDIDATE/hosts" ]; then
            FOUND="$CANDIDATE"; break
        fi
    done
    if [ -z "$FOUND" ]; then
        FOUND="$(find /root -maxdepth 3 \( -name ansible.cfg -o -name inventory.yaml \) \
                 -not -path '*/venv/*' -not -path "$BASE/*" 2>/dev/null | head -1 | xargs -r dirname || true)"
    fi
    if [ -n "$FOUND" ]; then
        "$VENV/bin/python" - "$CONFIG" "$FOUND" <<'PY'
import re, sys
config, found = sys.argv[1], sys.argv[2]
text = open(config, encoding="utf-8").read()
text = re.sub(r"(?m)^(  ansible_dir:).*$", r"\1 " + found, text, count=1)
open(config, "w", encoding="utf-8").write(text)
PY
        ok "Ansible-Verzeichnis erkannt und eingetragen: $FOUND"
        for NAME in inventory.yaml inventory.yml hosts inventory; do
            if [ -e "$FOUND/$NAME" ]; then
                "$VENV/bin/python" - "$CONFIG" "$NAME" <<'PY'
import re, sys
config, name = sys.argv[1], sys.argv[2]
text = open(config, encoding="utf-8").read()
text = re.sub(r"(?m)^(  inventory:).*$", r"\1 " + name, text, count=1)
open(config, "w", encoding="utf-8").write(text)
PY
                ok "Inventory erkannt: $NAME"
                break
            fi
        done
    else
        warn "Das Ansible-Verzeichnis konnte nicht erraten werden - bitte in der config.yaml unter paths.ansible_dir eintragen."
    fi
    warn "Bitte die config.yaml jetzt durchsehen: $CONFIG"
fi

if [ "$USE_TLS" -eq 1 ]; then
    if grep -q '^  tls:' "$CONFIG" && grep -q '^    enabled: false' "$CONFIG"; then
        "$VENV/bin/python" - "$CONFIG" <<'PY'
import re, sys
config = sys.argv[1]
text = open(config, encoding="utf-8").read()
text = text.replace("  tls:\n    enabled: false", "  tls:\n    enabled: true", 1)
open(config, "w", encoding="utf-8").write(text)
PY
        ok "HTTPS eingeschaltet"
    else
        ok "HTTPS ist bereits eingetragen"
    fi
fi

if [ -n "$BIND_HOST" ]; then
    "$VENV/bin/python" - "$CONFIG" "$BIND_HOST" <<'PY'
import re, sys
config, host = sys.argv[1], sys.argv[2]
text = open(config, encoding="utf-8").read()
text = re.sub(r'(?m)^(  host:).*$', r'\1 "%s"' % host, text, count=1)
open(config, "w", encoding="utf-8").write(text)
PY
    ok "Lauscht auf $BIND_HOST"
    if [ "$USE_TLS" -eq 0 ] && [ "$BIND_HOST" != "127.0.0.1" ]; then
        warn "Ohne HTTPS gehen Passwoerter im Klartext ueber das Netz. Erwaege --tls."
    fi
fi

chmod 600 "$CONFIG" 2>/dev/null || true
chmod +x "$BASE/bin/ansible-web-run" "$BASE/bin/ansible-web-serve" "$BASE/bin/ansible-web-admin" 2>/dev/null || true

# -- Pruefen und Benutzer ---------------------------------------------------

say "Konfiguration pruefen"
set +e
ANSIBLE_WEB_CONFIG="$CONFIG" "$VENV/bin/python" "$BASE/bin/ansible-web-admin" pruefen
CHECK=$?
set -e
[ "$CHECK" -le 1 ] || die "Die Konfiguration ist fehlerhaft."

TLS_ON="$(ANSIBLE_WEB_CONFIG="$CONFIG" "$VENV/bin/python" - <<PY
import sys; sys.path.insert(0, "$BASE")
from app.config import load_config
print("ja" if load_config().tls_enabled else "nein")
PY
)"

if [ "$TLS_ON" = "ja" ]; then
    say "Zertifikat vorbereiten"
    ANSIBLE_WEB_CONFIG="$CONFIG" "$VENV/bin/python" - <<PY
import sys; sys.path.insert(0, "$BASE")
from app.config import load_config
from app.tls import ensure_certificate
cfg = load_config()
info = ensure_certificate(cfg)
if info.get("error"):
    print("    Fehler: %s" % info["error"]); raise SystemExit(1)
print("    %s %s" % ("Neu erzeugt:" if info.get("generated") else "Vorhanden:", cfg.tls_cert))
print("    Gueltig fuer: %s" % ", ".join(info.get("sans") or []))
print("    Fingerabdruck (SHA-256):")
print("      %s" % info.get("fingerprint_sha256"))
PY
fi

if [ -n "$ADMIN" ]; then
    say "Administrator anlegen"
    ANSIBLE_WEB_CONFIG="$CONFIG" "$VENV/bin/python" "$BASE/bin/ansible-web-admin" \
        benutzer-anlegen "$ADMIN" --rolle admin
fi

# -- Abschluss --------------------------------------------------------------

HOSTPORT="$(ANSIBLE_WEB_CONFIG="$CONFIG" "$VENV/bin/python" - <<PY
import sys; sys.path.insert(0, "$BASE")
from app.config import load_config
print(load_config().base_url)
PY
)"

cat <<EOF

$(printf '\033[1;32m')Fertig.$(printf '\033[0m')

  Zum Ausprobieren im Vordergrund:
      $BASE/bin/ansible-web-serve

  Als Dienst (systemd):
      cp $BASE/deploy/ansible-web.service /etc/systemd/system/
      systemctl daemon-reload && systemctl enable --now ansible-web

  Im Container ohne systemd (supervisord):
      cp $BASE/deploy/supervisord-ansible-web.conf /etc/supervisor/conf.d/ansible-web.conf
      supervisorctl reread && supervisorctl update

  Danach im Browser:  $HOSTPORT

  Gibt es noch keinen Benutzer, fuehrt die Oberflaeche beim ersten Aufruf
  durch die Ersteinrichtung. Alternativ hier anlegen:
      $BASE/bin/ansible-web-admin benutzer-anlegen admin --rolle admin
$( [ "$TLS_ON" = "ja" ] && printf '%s' "
  Das Zertifikat ist selbstsigniert - der Browser warnt beim ersten Aufruf.
  Den oben gezeigten Fingerabdruck mit dem im Zertifikatsdialog vergleichen,
  dann die Ausnahme bestaetigen.
" )

EOF
