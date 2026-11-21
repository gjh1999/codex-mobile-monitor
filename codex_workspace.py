"""Relay live Codex alerts and decisions through the authenticated personal workspace."""

import json
import re
import socket
import threading
import time
from urllib.error import HTTPError

from automation import APPROVAL_METHODS, can_allow
from personal_relay import EMPTY, digest


PREFIX = 'codex-live/'
DEVICE = re.compile(r'[A-Za-z0-9_-]{1,100}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{32}')
HEARTBEAT = 8
LIVE = 35
RETENTION = 300


def valid_token(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def recent(value, now, age):
    return type(value) in (int, float) and now - age <= value <= now + LIVE


class CodexWorkspace:
    def __init__(self, approvals, clock=time.time):
        self.approvals = approvals
        self.clock = clock
        self.lock = threading.Lock()
        self.identity = None
        self.peers = {}
        self.remote = {}
        self.outgoing = {}
        self.incoming_results = {}
        self.results = {}
        self.completions = {}
        self.published = 0

    def set_identity(self, identity):
        with self.lock:
            if self.identity != identity:
                self.identity = identity
                self.peers.clear()
                self.remote.clear()
                self.outgoing.clear()
                self.incoming_results.clear()
                self.results.clear()
                self.published = 0

    def complete(self, event):
        key = event['threadId'], event['turnId']
        self.completions.setdefault(key, event)

    def pending(self):
        now = self.clock()
        with self.lock:
            return [{**row, 'decisionQueued': token in self.outgoing,
                     'canAllow': row['canAllow'] and token not in self.outgoing}
                    for token, row in self.remote.items()
                    if row['expires'] > now and row['liveUntil'] > now]

    def decide(self, token, allow):
        now = self.clock()
        with self.lock:
            if token in self.outgoing:
                return 'queued'
            row = self.remote.get(token)
            if row is None:
                return 'not_found'
            if row['expires'] <= now or row['liveUntil'] <= now:
                return 'expired'
            if allow and not row['canAllow']:
                return 'details_unavailable'
            self.results.pop(token, None)
            self.outgoing[token] = {'target': row['device'], 'token': row['sourceToken'], 'allow': allow,
                                    'time': now, 'expires': row['expires']}
            return 'queued'

    def decisions(self):
        now = self.clock()
        with self.lock:
            return {token: decision for token, (decision, when) in self.results.items()
                    if recent(when, now, RETENTION)}

    def peer_records(self, store, index, device, now):
        cache = {}
        for key, revision in index.items():
            peer = key.removeprefix(PREFIX)
            if peer == key or peer == device or not DEVICE.fullmatch(peer) or revision == EMPTY:
                continue
            prior = self.peers.get(peer)
            try:
                row = prior[1] if prior and prior[0] == revision else json.loads(store.read(key))
                if not isinstance(row, dict) or row.get('v') != 1:
                    continue
                if recent(row.get('time'), now, LIVE):
                    cache[peer] = revision, row
            except HTTPError as error:
                if error.code != 404:
                    raise
            except (TypeError, ValueError, UnicodeError):
                continue
        self.peers = cache
        return {peer: row for peer, (_, row) in cache.items()}

    @staticmethod
    def entries(record, field):
        values = record.get(field)
        return (entry for entry in values if isinstance(entry, dict)) if isinstance(values, list) else ()

    def receive(self, records, device, now):
        remote, turns = {}, []
        for peer, row in records.items():
            name = row.get('name')
            name = name[:80] if isinstance(name, str) and name else peer
            turns.extend(self.entries(row, 'completions'))
            for item in self.entries(row, 'approvals'):
                source, expires, details = item.get('token'), item.get('expires'), item.get('details')
                if (not valid_token(source) or type(expires) not in (int, float) or not now < expires <= now + RETENTION + LIVE
                        or not isinstance(details, dict) or item.get('method') not in APPROVAL_METHODS):
                    continue
                token = f'remote:{peer}:{source}'
                remote[token] = {'token': token, 'sourceToken': source, 'client': 'codex', 'method': item['method'],
                                 'details': details, 'device': peer, 'deviceName': name, 'remote': True, 'expires': expires,
                                 'decision': None, 'canAllow': can_allow(details), 'liveUntil': row['time'] + LIVE}
            for item in self.entries(row, 'decisions'):
                source = item.get('token')
                key = peer, source
                if (item.get('target') == device and valid_token(source) and type(item.get('allow')) is bool
                        and recent(item.get('time'), now, RETENTION) and key not in self.incoming_results):
                    decision = self.approvals.decide_pending(source, item['allow'], client='codex')
                    self.incoming_results[key] = {'target': peer, 'token': source, 'decision': decision, 'time': now}
            for item in self.entries(row, 'receipts'):
                source, decision = item.get('token'), item.get('decision')
                if item.get('target') == device and valid_token(source) and isinstance(decision, str):
                    token = f'remote:{peer}:{source}'
                    with self.lock:
                        if token in self.outgoing:
                            self.outgoing.pop(token)
                            self.results[token] = decision[:60], now
        with self.lock:
            self.remote = remote
        return turns

    def sync(self, store):
        now = self.clock()
        device = store.client.device
        if not DEVICE.fullmatch(device):
            raise ValueError('设备标识无效')
        index = store.index()
        turns = self.receive(self.peer_records(store, index, device, now), device, now)
        self.completions = {key: item for key, item in self.completions.items() if recent(item['time'], now, RETENTION)}
        self.incoming_results = {key: item for key, item in self.incoming_results.items()
                                 if recent(item['time'], now, RETENTION)}
        with self.lock:
            for token in list(self.outgoing):
                if self.outgoing[token]['expires'] <= now:
                    del self.outgoing[token]
                    self.results[token] = 'expired', now
            self.results = {key: value for key, value in self.results.items() if recent(value[1], now, RETENTION)}
            outgoing = list(self.outgoing.values())
        data = {'v': 1, 'time': self.published, 'name': socket.gethostname()[:80], 'decisions': outgoing,
                'receipts': list(self.incoming_results.values()), 'completions': list(self.completions.values()),
                'approvals': self.approvals.pending('codex')}
        key = PREFIX + device
        previous = index.get(key, EMPTY)
        encode = lambda: json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
        if now - self.published >= HEARTBEAT or digest(encode()) != previous:
            data['time'] = now
            store.put(key, previous, encode())
            self.published = now
        return turns
