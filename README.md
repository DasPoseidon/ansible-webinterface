Achtung: 100% Vibecode

# Ansible-Webinterface

Eine Weboberfläche für einen bestehenden Ansible-Controller: Playbooks starten
(gegen das ganze Inventory oder ausgewählte Hosts), live verfolgen, welcher Host
gerade bei welchem Schritt ist, Playbooks und `inventory.yaml` bearbeiten,
Konfigurations- und Datendateien verwalten und die Cron-Jobs pflegen.

Die Oberfläche ersetzt die Kommandozeile nicht, sie legt sich daneben:
Playbooks werden als ganz normaler `ansible-playbook`-Prozess im echten
Ansible-Verzeichnis gestartet. `ansible.cfg`, Rollenpfade, relative Pfade,
SSH-Einstellungen und Vault gelten damit unverändert. Was von Hand läuft,
läuft hier genauso – nur sichtbar.

---

## Inhalt

1. [Voraussetzungen](#voraussetzungen)
2. [Installation](#installation)
3. [Als Dienst einrichten](#als-dienst-einrichten)
4. [Erster Start](#erster-start)
5. [Die Bereiche im Einzelnen](#die-bereiche-im-einzelnen)
6. [Geplante Läufe (Cron)](#geplante-läufe-cron)
7. [Rollen und Rechte](#rollen-und-rechte)
8. [Sicherheit](#sicherheit)
9. [Verzeichnisse und Dateien](#verzeichnisse-und-dateien)
10. [Konfiguration](#konfiguration)
11. [Betrieb](#betrieb)
12. [Fehlersuche](#fehlersuche)

---

## Voraussetzungen

* Linux mit **Python 3.9 oder neuer** (inklusive `python3-venv`)
* **Ansible** ist bereits installiert und funktioniert auf der Kommandozeile
* **cron** für geplante Läufe (optional, aber für den Zeitplan-Bereich nötig)
* **git** (optional) – liegt das Ansible-Verzeichnis in einem Repository, wird
  jede Änderung über die Oberfläche als eigener Commit festgehalten

Internetzugang wird nur einmal bei der Installation gebraucht, um die
Python-Pakete zu holen. Danach läuft alles ohne externe Dienste; auch die
Oberfläche selbst lädt nichts aus dem Netz nach.

---

## Installation

Das Verzeichnis nach `/root/ansible-web` kopieren und das Einrichtungsskript
starten:

```bash
cd /root/ansible-web
./setup.sh
```

Das Skript prüft die Voraussetzungen, legt unter `venv/` eine eigene virtuelle
Umgebung an (die vorhandene Ansible-Installation bleibt unberührt), installiert
die Abhängigkeiten und erzeugt aus der Vorlage eine `config.yaml`. Dabei
versucht es, das Ansible-Verzeichnis und das Inventory selbst zu finden.

Danach die Konfiguration durchsehen – vor allem diese drei Angaben:

```yaml
paths:
  ansible_dir: /root/ansible        # Verzeichnis mit den Playbooks
  inventory: inventory.yaml         # relativ dazu oder absolut
  data_dir: files                   # Unterordner mit Konfig-/Datendateien
```

Ob alles passt, sagt:

```bash
./bin/ansible-web-admin pruefen
```

Ein erneuter Aufruf von `./setup.sh` ist jederzeit möglich; eine vorhandene
`config.yaml` wird nie überschrieben.

---

## Als Dienst einrichten

**Mit systemd:**

```bash
cp /root/ansible-web/deploy/ansible-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ansible-web
systemctl status ansible-web
```

**Im Container ohne systemd (supervisord):**

```bash
cp /root/ansible-web/deploy/supervisord-ansible-web.conf /etc/supervisor/conf.d/ansible-web.conf
supervisorctl reread && supervisorctl update
```

**Ohne beides** – etwa direkt aus dem Startskript des Containers:

```bash
/root/ansible-web/bin/ansible-web-serve
```

Standardmäßig lauscht der Dienst nur auf `127.0.0.1:8080`. Soll er im Netz
erreichbar sein, entweder `server.host` auf `0.0.0.0` setzen oder – besser –
einen Reverse Proxy mit TLS davorstellen. Eine Beispielkonfiguration für nginx
liegt unter `deploy/nginx-ansible-web.conf`; die dort gesetzten
Upgrade-Header sind nötig, sonst bleibt die Live-Ansicht leer.

---

## Erster Start

Beim ersten Aufruf im Browser führt die Oberfläche durch die Ersteinrichtung
und legt den ersten Administrator an. Diese Maske ist nur erreichbar, solange
es überhaupt keinen Benutzer gibt.

Wer den Benutzer lieber vorher anlegt:

```bash
./bin/ansible-web-admin benutzer-anlegen admin --rolle admin
```

Weitere Benutzer legt man danach unter **Verwaltung → Benutzer** an. Falls
später niemand mehr hineinkommt, hilft dasselbe Programm:

```bash
./bin/ansible-web-admin benutzer-liste
./bin/ansible-web-admin passwort-setzen admin
./bin/ansible-web-admin rolle-setzen anna admin
```

---

## Die Bereiche im Einzelnen

### Läufe

Verlauf aller Ausführungen – über die Oberfläche gestartete ebenso wie
geplante. Aktive Läufe stehen oben mit Fortschrittsbalken. Gefiltert wird nach
Zustand, Auslöser und Freitext.

### Playbook starten

Links die gefundenen Playbooks mit Anzahl der Plays und Schritte. Rechts die
Optionen. Das Ziel wird auf drei Arten festgelegt:

* **Ganzes Inventory** – kein `--limit`
* **Gruppen & Hosts** – Baum aus dem Inventory zum Anhaken
* **Eigener Ausdruck** – die volle `--limit`-Syntax, etwa
  `webservers:!web03.example.com`

In allen Fällen wird unter der Auswahl angezeigt, welche Hosts tatsächlich
getroffen würden – das wird von Ansible selbst beantwortet, nicht geraten.
Daneben stehen `--tags`, `--skip-tags`, Probelauf (`--check`),
Änderungsanzeige (`--diff`), `--forks`, Ausführlichkeit und Zusatzvariablen.

Die Vorschau zeigt vor dem Start, welche Schritte in welchem Play ausgeführt
würden. Vor einem echten (also nicht als Probelauf markierten) Start wird
nachgefragt.

### Live-Ansicht

Der Kern. Während ein Playbook läuft:

* **Hosts** – eine Karte je Host mit Zustand, aktuellem Schritt und den
  Zählern für ok / geändert / fehlgeschlagen / übersprungen / nicht erreichbar.
  Die Hosts stehen schon zu Beginn eines Plays fest, nicht erst wenn die erste
  Meldung eintrifft.
* **Schritte** – die Zeitfolge aller Tasks, gruppiert nach Play, mit einem
  Punkt je Host und Dauer. Ein Klick öffnet die Details: Ergebnis, Dauer,
  Rückgabecode und Meldung je Host, dazu Schleifen-Elemente, Diffs und auf
  Wunsch die Rohereignisse.
* **Protokoll** – die unveränderte Ausgabe von Ansible samt Farben, mit Suche,
  Filter auf Auffälligkeiten und Mitlaufen beim Scrollen.

Die Übertragung läuft über eine WebSocket-Verbindung; bricht sie ab, verbindet
sich die Seite von selbst neu und holt den verpassten Stand nach. Ein Lauf
lässt sich abbrechen – Ansible bekommt zuerst ein SIGINT und räumt selbst auf,
erst danach wird härter nachgefasst.

Die Daten dafür liefert ein mitgeliefertes Callback-Plugin
(`callback_plugins/ansible_web_events.py`) vom Typ *notification*. Es ersetzt
die normale Ausgabe nicht, sondern schreibt zusätzlich strukturierte Ereignisse
mit. Bereits in der `ansible.cfg` aktivierte Callbacks bleiben aktiv.

### Playbooks & Inventory

Editor mit Syntaxhervorhebung für Playbooks, `inventory.yaml`, `group_vars`,
`host_vars` und `ansible.cfg`. Beim Speichern passiert der Reihe nach:

1. **Prüfen** – YAML wird geparst (auch doppelte Schlüssel werden gemeldet,
   die YAML stillschweigend überschreibt), Playbooks zusätzlich mit
   `ansible-playbook --syntax-check`, das Inventory mit `ansible-inventory`.
   Geprüft wird gegen eine temporäre Datei; schlägt die Prüfung fehl, bleibt
   die Originaldatei unangetastet.
2. **Sichern** – eine Kopie mit Zeitstempel unter `state/backups/`.
3. **Schreiben** – atomar, mit Erhalt von Rechten und Eigentümer.
4. **Committen** – ein Git-Commit mit dem angemeldeten Benutzer als Autor.

Über die Leiste erreichbar: Historie (Commits der Datei mit Diff und
„Fassung laden"), Sicherungskopien und eine reine Prüfung ohne Speichern.
Hat jemand anderes die Datei zwischenzeitlich geändert, wird das erkannt und
die Entscheidung abgefragt, statt die fremde Änderung zu überschreiben.

### Dateien

Der Unterordner mit Konfigurationsdateien und Daten (`paths.data_dir`), von
dem aus sich das ganze Ansible-Verzeichnis durchsehen lässt. Textdateien lassen
sich bearbeiten (mit derselben Prüf- und Sicherungslogik), beliebige Dateien
herunter- und hochladen (auch per Drag & Drop), Ordner als ZIP herunterladen,
umbenennen, löschen und anlegen.

Alle Pfade sind fest auf das Ansible-Verzeichnis begrenzt: `..`, absolute
Pfade und Verweise, die nach draußen zeigen, werden abgewiesen. Das
`.git`-Verzeichnis ist gesperrt.

### Zeitplan (Cron)

Siehe nächster Abschnitt.

### Verwaltung

Zustand (Pfade, Ansible, Git, Läufe), Benutzer, API-Tokens, das Audit-Protokoll
und die Änderung des eigenen Passworts.

---

## Geplante Läufe (Cron)

Die Jobs stehen in der crontab von **root**, in einem klar markierten Block:

```crontab
# >>> ansible-web verwaltet - nicht per Hand bearbeiten >>>
# ansible-web: id=9151b924 name='Nächtlicher Deploy' enabled=true
30 2 * * 1-5 /root/ansible-web/bin/ansible-web-run --playbook deploy-app.yml \
    --name 'Nächtlicher Deploy' --limit webservers --tags deploy \
    >> /root/ansible-web/state/cron.log 2>&1
# <<< ansible-web verwaltet <<<
```

Alles außerhalb dieses Blocks – andere Cron-Jobs, `MAILTO`, Kommentare –
bleibt unangetastet und wird in der Oberfläche nur angezeigt. Vor jeder
Änderung wird die bisherige crontab unter `state/crontab-backups/` gesichert.

Die Zeilen rufen den Client `bin/ansible-web-run` auf. Der meldet den Lauf beim
Dienst an, gibt die Ausgabe weiterhin auf stdout aus (die gewohnte Cron-Mail
enthält also alles) und endet mit dem Rückgabecode von `ansible-playbook`.
Dadurch erscheint jeder geplante Lauf mit Host-Status, Schritten und Protokoll
in der Historie.

**Wenn der Dienst gerade nicht läuft**, ruft der Client `ansible-playbook`
direkt auf. Der Termin fällt also nicht aus. Der Lauf wird dabei vollständig
mitgeschrieben und taucht beim nächsten Start des Dienstes in der Historie auf,
gekennzeichnet als „Notbetrieb". Abschalten lässt sich das mit `--no-fallback`.

### Bestehende Cron-Zeilen übernehmen

Vorhandene Ansible-Zeilen in der crontab erkennt die Oberfläche und bietet
„übernehmen" an. Dabei wird Zeitplan, Playbook, `--limit`, Tags und Optionen
aus der Zeile gelesen, die Zeile aus ihrer bisherigen Position entfernt und als
verwalteter Job neu geschrieben. Inhaltlich ändert sich nichts – der Lauf wird
künftig nur zusätzlich protokolliert.

Zeilen, aus denen sich kein Playbook ablesen lässt (etwa `ansible … -m ping`),
werden angezeigt, aber nicht zur Übernahme angeboten; solche Jobs legt man
besser von Hand neu an.

### Zeitplan

Der Zeitplan nimmt die übliche Cron-Schreibweise und die Kürzel `@daily`,
`@hourly`, `@weekly`, `@monthly`, `@yearly`, `@reboot`. Er wird beim Eingeben
geprüft, in Worte übersetzt („montags bis freitags um 02:30") und die nächsten
Termine werden angezeigt.

Wer lieber direkt schreibt: unter **crontab im Rohtext** lässt sich die ganze
Datei bearbeiten – mit Prüfung und Sicherungskopie.

---

## Rollen und Rechte

| Rolle | Darf |
|---|---|
| `viewer` | alles ansehen: Läufe, Protokolle, Dateien, Inventory, Zeitplan |
| `operator` | zusätzlich Playbooks starten und abbrechen |
| `editor` | zusätzlich Dateien, Playbooks und Inventory ändern, hochladen, löschen, committen |
| `admin` | zusätzlich Cron-Jobs, Benutzer, Tokens und das Audit-Protokoll |

Jede Aktion landet im Audit-Protokoll (`state/audit.ndjson`): wer, wann, was,
mit Erfolg oder nicht – abgewiesene Zugriffe eingeschlossen.

---

## Sicherheit

Dieses Werkzeug führt Playbooks als **root** aus. Wer Zugang zur Oberfläche
hat, hat damit faktisch Root-Rechte auf dem Controller und allen verwalteten
Hosts. Entsprechend:

* **Nicht ungeschützt ins Internet stellen.** Die Vorgabe ist `127.0.0.1`.
  Für den Zugriff von außen gehört ein Reverse Proxy mit TLS davor, und dann
  `server.secure_cookies: true` und `server.behind_proxy: true` setzen.
* **Passwörter** werden mit PBKDF2-SHA256 (210.000 Runden) gespeichert,
  niemals im Klartext. Wiederholte Fehlversuche werden pro IP und pro
  Benutzername gebremst.
* **Sitzungen** liegen serverseitig, das Cookie ist `HttpOnly` und
  `SameSite=Lax`. Schreibende Zugriffe verlangen zusätzlich ein
  CSRF-Merkmal im HTTP-Header – bewusst nicht aus dem Cookie, denn das
  schickt der Browser auch bei Anfragen von fremden Seiten mit.
* **WebSocket-Verbindungen** werden angemeldet und ihre Herkunft geprüft.
* **Dateizugriffe** sind fest auf das Ansible-Verzeichnis begrenzt.
* **Geheimnisse** (`state/auth.json`, `state/client.json`, `config.yaml`)
  haben die Rechte 0600.

Was das Werkzeug **nicht** leistet: es prüft nicht, ob ein Playbook sinnvoll
ist. Ein Operator, der ein Playbook starten darf, kann alles tun, was dieses
Playbook tut.

---

## Verzeichnisse und Dateien

```
/root/ansible-web/
├── app/                      Anwendung (Python)
├── callback_plugins/
│   └── ansible_web_events.py Callback für die Live-Ereignisse
├── bin/
│   ├── ansible-web-serve     startet den Dienst
│   ├── ansible-web-run       Client für die Cron-Zeilen
│   └── ansible-web-admin     Benutzer und Tokens von der Kommandozeile
├── deploy/                   systemd-Unit, supervisord, nginx-Beispiel
├── static/                   Oberfläche (ohne Build-Schritt, nichts aus dem Netz)
├── config.example.yaml       kommentierte Vorlage
├── config.yaml               die eigene Konfiguration (0600)
├── requirements.txt
├── setup.sh
└── venv/                     eigene Python-Umgebung

<state_dir>/                  voreingestellt /root/ansible-web/state
├── runs/<lauf-id>/           meta.json, events.ndjson, output.log, command.txt
├── backups/<pfad>/<stempel>  Sicherungskopien geänderter Dateien
├── crontab-backups/          crontab vor jeder Änderung
├── cron-vars/                Zusatzvariablen geplanter Jobs
├── auth.json                 Benutzer und Token-Hashes (0600)
├── sessions.json             offene Sitzungen (0600)
├── client.json               Zugangsdaten des Cron-Clients (0600)
├── audit.ndjson              Audit-Protokoll
└── cron.log                  gesammelte Ausgabe der Cron-Läufe
```

Das Zustandsverzeichnis sollte **nicht** im Ansible-Verzeichnis liegen – sonst
landen Protokolle und Sicherungen in Git.

---

## Konfiguration

Alle Einstellungen stehen kommentiert in `config.example.yaml`. Die häufigsten:

| Einstellung | Bedeutung |
|---|---|
| `server.host` / `server.port` | Adresse, auf der gelauscht wird |
| `paths.ansible_dir` | Verzeichnis mit den Playbooks |
| `paths.inventory` | Inventory-Datei oder -Verzeichnis |
| `paths.data_dir` | Unterordner mit Konfig-/Datendateien |
| `run.max_concurrent` | wie viele Läufe gleichzeitig (Vorgabe 1, Rest wartet) |
| `run.keep_runs` | wie viele Läufe aufbewahrt werden |
| `run.vault_password_file` | Datei mit dem Vault-Passwort |
| `run.extra_env` | zusätzliche Umgebungsvariablen für jeden Lauf |
| `files.max_upload_bytes` | Obergrenze für Uploads |
| `git.enabled` / `git.push` | Git-Anbindung, automatisches Pushen |

Die `config.yaml` wird nur gelesen, nie zurückgeschrieben – Kommentare bleiben
erhalten. Nach einer Änderung den Dienst neu starten.

**Vault:** Playbooks, die ein Passwort abfragen würden (`--ask-vault-pass`,
`vars_prompt`, `--ask-become-pass`), können hier nicht laufen – die Eingabe ist
absichtlich auf `/dev/null` gelegt, damit ein Lauf nicht unbemerkt hängt.
Stattdessen `run.vault_password_file` setzen oder mit SSH-Schlüsseln und
passwortlosem `become` arbeiten.

---

## Betrieb

**Protokolle des Dienstes:** `journalctl -u ansible-web -f` (systemd) bzw.
`/var/log/ansible-web.log` (supervisord).

**Sichern:** es genügt, `config.yaml` und das Zustandsverzeichnis zu sichern.
Das Ansible-Verzeichnis selbst liegt ohnehin in Git.

**Aktualisieren:** neue Dateien über die alten kopieren (`config.yaml` und
`state/` dabei stehen lassen), dann `./setup.sh --no-venv` für die
Abhängigkeiten und den Dienst neu starten.

**Aufräumen** geschieht von selbst: alte Läufe werden nach `run.keep_runs`
entfernt, Sicherungskopien nach `files.keep_backups`, crontab-Sicherungen nach
40 Ständen.

**Beim Neustart** laufende Playbooks: Ansible-Prozesse bekommen ein SIGTERM.
Ein Lauf, der dadurch abbricht, wird als „unterbrochen" gekennzeichnet – was
bis dahin passiert ist, steht im Protokoll, aber der tatsächliche Zustand der
Hosts ist dann offen und sollte geprüft werden.

---

## Fehlersuche

**Die Live-Ansicht bleibt leer, das Protokoll kommt erst am Ende.**
Ein Reverse Proxy reicht die WebSocket-Verbindung nicht durch. Die drei
Upgrade-Header aus `deploy/nginx-ansible-web.conf` fehlen.

**„crontab wurde nicht gefunden".**
Im Container ist kein cron installiert. Alles andere funktioniert weiter, nur
der Zeitplan-Bereich nicht.

**Playbooks tauchen nicht in der Liste auf.**
Gesucht wird nach `discovery.playbook_globs` relativ zu `ansible_dir`. Als
Playbook gilt eine YAML-Datei, die eine Liste von Plays enthält (`hosts:` oder
`import_playbook:`). Dateien mit YAML-Fehlern werden trotzdem angezeigt, damit
man sie reparieren kann.

**„Inventory nicht lesbar".**
Was die Oberfläche anzeigt, ist die Ausgabe von `ansible-inventory` selbst.
Zum Gegenprüfen: `cd /root/ansible && ansible-inventory -i inventory.yaml --list`.

**Ein Lauf hängt ohne Ausgabe.**
Vermutlich wartet das Playbook auf eine Eingabe (`vars_prompt`,
`--ask-vault-pass`). Den Lauf abbrechen und das Playbook so umstellen, dass es
ohne Rückfrage auskommt.

**Niemand kommt mehr hinein.**
`./bin/ansible-web-admin passwort-setzen admin` auf dem Controller.

**Ein geplanter Lauf fehlt in der Historie.**
In `state/cron.log` nachsehen. Steht dort „Webinterface nicht erreichbar", lief
der Job im Notbetrieb – er erscheint, sobald der Dienst wieder startet.
