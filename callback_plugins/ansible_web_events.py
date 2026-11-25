# -*- coding: utf-8 -*-
"""Callback-Plugin fuer das Ansible-Webinterface.

Schreibt strukturierte Ereignisse als NDJSON in die Datei aus
``ANSIBLE_WEB_EVENT_FILE``. Das Plugin ist vom Typ ``notification`` und
ersetzt damit *nicht* die normale Konsolenausgabe - der gewohnte Output
bleibt also vollstaendig erhalten.

Das Plugin darf einen Playbook-Lauf unter keinen Umstaenden stoeren:
jeder Hook ist gegen Ausnahmen abgesichert, und wenn die Ereignisdatei
nicht beschreibbar ist, arbeitet Ansible einfach ohne Ereignisse weiter.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import time

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = '''
    name: ansible_web_events
    type: notification
    short_description: schreibt Lauf-Ereignisse als NDJSON fuer das Webinterface
    description:
      - Schreibt pro Zeile ein JSON-Objekt mit Play-, Task- und Host-Ereignissen.
      - Zieldatei kommt aus der Umgebungsvariablen ANSIBLE_WEB_EVENT_FILE.
    requirements:
      - aktiviert ueber callbacks_enabled
'''

MAX_TEXT = 8000
MAX_ITEM = 1200
MAX_DIFF = 20000


def _truncate(value, limit=MAX_TEXT):
    """Kuerzt lange Texte und markiert die Kuerzung."""
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    if len(value) > limit:
        return value[:limit] + "\n... [gekuerzt, %d Zeichen insgesamt]" % len(value)
    return value


def _safe(value, limit=MAX_ITEM):
    """Macht beliebige Werte JSON-serialisierbar."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        return _truncate(json.dumps(value, ensure_ascii=False, default=str), limit)
    except Exception:
        return _truncate(str(value), limit)


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'ansible_web_events'
    CALLBACK_NEEDS_ENABLED = True
    CALLBACK_NEEDS_WHITELIST = True   # Kompatibilitaet mit aelteren Versionen

    def __init__(self, *args, **kwargs):
        super(CallbackModule, self).__init__(*args, **kwargs)
        self._handle = None
        self._seq = 0
        self._task_started = {}      # (host, task_uuid) -> Startzeit
        self._current_task = None
        self._current_play = None
        self._run_id = os.environ.get('ANSIBLE_WEB_RUN_ID', '')
        path = os.environ.get('ANSIBLE_WEB_EVENT_FILE')
        if path:
            try:
                self._handle = open(path, 'a', buffering=1, encoding='utf-8')
            except Exception:
                self._handle = None

    # -- Ausgabe ---------------------------------------------------------

    def _emit(self, event, **fields):
        if self._handle is None:
            return
        self._seq += 1
        record = {'seq': self._seq, 't': time.time(), 'e': event}
        record.update(fields)
        try:
            self._handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            try:
                self._handle.write(json.dumps({'seq': self._seq, 't': time.time(),
                                               'e': 'event_error', 'orig': event}) + "\n")
            except Exception:
                pass

    # -- Hilfsfunktionen -------------------------------------------------

    @staticmethod
    def _task_uuid(task):
        try:
            return str(task._uuid)
        except Exception:
            return ''

    @staticmethod
    def _host_name(result):
        try:
            return result._host.get_name()
        except Exception:
            return '?'

    def _duration(self, host, task_uuid):
        started = self._task_started.pop((host, task_uuid), None)
        if started is None:
            return None
        return round(time.time() - started, 3)

    @staticmethod
    def _no_log(result):
        try:
            if getattr(result._task, 'no_log', False):
                return True
        except Exception:
            pass
        try:
            return bool(result._result.get('_ansible_no_log'))
        except Exception:
            return False

    def _result_payload(self, result, status):
        """Baut die Nutzdaten eines Host-Ergebnisses, no_log wird respektiert."""
        payload = {}
        if self._no_log(result):
            payload['censored'] = True
            return payload
        try:
            res = result._result or {}
        except Exception:
            return payload

        for key in ('msg', 'stdout', 'stderr', 'reason'):
            value = res.get(key)
            if value not in (None, '', [], {}):
                payload[key] = _truncate(value)
        for key in ('rc', 'changed', 'skip_reason', 'warnings'):
            if key in res:
                payload[key] = _safe(res.get(key))
        if res.get('results') and isinstance(res.get('results'), list):
            payload['item_count'] = len(res['results'])
        if 'item' in res:
            payload['item'] = _safe(res.get('item'))
        if res.get('exception'):
            payload['exception'] = _truncate(res.get('exception'), 2000)
        delegated = res.get('_ansible_delegated_vars') or {}
        if isinstance(delegated, dict) and delegated.get('ansible_host'):
            payload['delegated_to'] = _safe(delegated.get('ansible_host'), 120)
        if status == 'failed' and 'stdout_lines' in res and 'stdout' not in payload:
            payload['stdout'] = _truncate("\n".join(str(x) for x in res.get('stdout_lines') or []))
        return payload

    # -- Playbook / Play / Task ------------------------------------------

    def v2_playbook_on_start(self, playbook):
        try:
            name = playbook._file_name
        except Exception:
            name = ''
        self._emit('playbook_start', playbook=str(name), run_id=self._run_id, pid=os.getpid())

    def v2_playbook_on_play_start(self, play):
        try:
            uuid = str(play._uuid)
        except Exception:
            uuid = ''
        name = play.get_name() or ''
        pattern = []
        try:
            raw = play.hosts
            pattern = list(raw) if isinstance(raw, (list, tuple)) else [str(raw)]
        except Exception:
            pass
        hosts = []
        try:
            inventory = play.get_variable_manager()._inventory
            hosts = sorted({h.get_name() for h in inventory.get_hosts(pattern)})
        except Exception:
            hosts = []
        self._current_play = uuid
        self._emit('play_start', play_uuid=uuid, name=name,
                   pattern=[str(p) for p in pattern], hosts=hosts,
                   check_mode=bool(getattr(play, 'check_mode', False)))

    def _on_task(self, task, is_handler=False):
        uuid = self._task_uuid(task)
        self._current_task = uuid
        path = ''
        try:
            path = task.get_path() or ''
        except Exception:
            pass
        action = ''
        try:
            action = str(task.action)
        except Exception:
            pass
        self._emit('task_start', task_uuid=uuid, play_uuid=self._current_play,
                   name=task.get_name() or action, action=action, path=str(path),
                   is_handler=bool(is_handler), no_log=bool(getattr(task, 'no_log', False)))

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._on_task(task, is_handler=False)

    def v2_playbook_on_handler_task_start(self, task):
        self._on_task(task, is_handler=True)

    def v2_playbook_on_cleanup_task_start(self, task):
        self._on_task(task, is_handler=False)

    def v2_playbook_on_include(self, included_file):
        try:
            hosts = sorted({h.get_name() for h in included_file._hosts})
        except Exception:
            hosts = []
        try:
            filename = str(included_file._filename)
        except Exception:
            filename = ''
        self._emit('include', file=filename, hosts=hosts)

    def v2_playbook_on_no_hosts_matched(self):
        self._emit('no_hosts_matched')

    def v2_playbook_on_no_hosts_remaining(self):
        self._emit('no_hosts_remaining')

    def v2_playbook_on_notify(self, handler, host):
        try:
            self._emit('notify', host=str(host), handler=handler.get_name())
        except Exception:
            pass

    # -- Host-Ereignisse --------------------------------------------------

    def v2_runner_on_start(self, host, task):
        """Ein Host beginnt einen Task - Basis fuer 'wer ist wo'."""
        try:
            name = host.get_name()
        except Exception:
            name = str(host)
        uuid = self._task_uuid(task)
        self._task_started[(name, uuid)] = time.time()
        self._emit('host_task_start', host=name, task_uuid=uuid,
                   task_name=task.get_name() or '')

    def _on_result(self, result, status, **extra):
        host = self._host_name(result)
        uuid = self._task_uuid(getattr(result, '_task', None)) if getattr(result, '_task', None) else ''
        payload = self._result_payload(result, status)
        self._emit('host_result', host=host, task_uuid=uuid, status=status,
                   duration=self._duration(host, uuid), data=payload, **extra)

    def v2_runner_on_ok(self, result):
        changed = False
        try:
            changed = bool((result._result or {}).get('changed'))
        except Exception:
            pass
        self._on_result(result, 'changed' if changed else 'ok')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._on_result(result, 'failed', ignore_errors=bool(ignore_errors))

    def v2_runner_on_skipped(self, result):
        self._on_result(result, 'skipped')

    def v2_runner_on_unreachable(self, result):
        self._on_result(result, 'unreachable')

    def v2_runner_on_async_poll(self, result):
        pass

    def v2_runner_retry(self, result):
        try:
            res = result._result or {}
            attempt = res.get('attempts')
            retries = res.get('retries')
        except Exception:
            attempt = retries = None
        self._emit('retry', host=self._host_name(result),
                   task_uuid=self._task_uuid(result._task) if getattr(result, '_task', None) else '',
                   attempt=attempt, retries=retries)

    # -- Schleifen-Elemente ----------------------------------------------

    def _on_item(self, result, status):
        host = self._host_name(result)
        uuid = self._task_uuid(result._task) if getattr(result, '_task', None) else ''
        payload = self._result_payload(result, status)
        self._emit('item_result', host=host, task_uuid=uuid, status=status, data=payload)

    def v2_runner_item_on_ok(self, result):
        changed = False
        try:
            changed = bool((result._result or {}).get('changed'))
        except Exception:
            pass
        self._on_item(result, 'changed' if changed else 'ok')

    def v2_runner_item_on_failed(self, result):
        self._on_item(result, 'failed')

    def v2_runner_item_on_skipped(self, result):
        self._on_item(result, 'skipped')

    # -- Diffs und Abschluss ---------------------------------------------

    def v2_on_file_diff(self, result):
        if self._no_log(result):
            return
        try:
            diff = (result._result or {}).get('diff')
        except Exception:
            return
        if not diff:
            return
        entries = diff if isinstance(diff, list) else [diff]
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = {}
            for key in ('before', 'after', 'before_header', 'after_header', 'prepared'):
                if entry.get(key) is not None:
                    item[key] = _truncate(entry.get(key), MAX_DIFF // max(1, len(entries)))
            if entry.get('dst') or entry.get('src'):
                item['path'] = _safe(entry.get('dst') or entry.get('src'), 300)
            if item:
                cleaned.append(item)
        if cleaned:
            self._emit('diff', host=self._host_name(result),
                       task_uuid=self._task_uuid(result._task) if getattr(result, '_task', None) else '',
                       diff=cleaned)

    def v2_playbook_on_stats(self, stats):
        summary = {}
        try:
            hosts = sorted(stats.processed.keys())
        except Exception:
            hosts = []
        for host in hosts:
            try:
                summary[host] = stats.summarize(host)
            except Exception:
                continue
        self._emit('stats', hosts=summary)
        if self._handle is not None:
            try:
                self._handle.flush()
            except Exception:
                pass
