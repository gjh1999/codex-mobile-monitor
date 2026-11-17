"""Multi-client manager supporting both Codex and Claude Code."""
import json
import threading
import time
from typing import Dict, List, Optional, Any
from universal_rpc import CodexRPC, ClaudeCodeRPC, RPCError
from automation import Approvals, Scheduler


class ClientManager:
    """Manages multiple RPC clients (Codex, Claude Code, etc)."""

    def __init__(self, root_path):
        self.root = root_path
        self.clients: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.approvals = Approvals()

    def connect(self, client_type: str, journal=None) -> bool:
        """
        Connect to a client.
        Args:
            client_type: 'codex' or 'claude'
        Returns:
            True if connected successfully
        """
        with self.lock:
            if client_type in self.clients and not self.clients[client_type].get('closed'):
                return True

            try:
                if client_type == 'codex':
                    rpc = CodexRPC(self.approvals, journal=journal)
                elif client_type == 'claude':
                    rpc = ClaudeCodeRPC(approvals=self.approvals, journal=journal)
                else:
                    return False

                self.clients[client_type] = {
                    'rpc': rpc,
                    'type': client_type,
                    'connected_at': time.time(),
                    'closed': False,
                    'threads': [],
                    'quota': None
                }
                return True
            except Exception as e:
                self.clients[client_type] = {
                    'error': str(e),
                    'closed': True,
                    'type': client_type
                }
                return False

    def disconnect(self, client_type: str):
        """Disconnect from a client."""
        with self.lock:
            if client_type in self.clients:
                rpc = self.clients[client_type].get('rpc')
                if rpc:
                    rpc.close()
                self.clients[client_type]['closed'] = True

    def get_rpc(self, client_type: str) -> Optional[Any]:
        """Get RPC client."""
        with self.lock:
            client = self.clients.get(client_type)
            return client.get('rpc') if client else None

    def list_clients(self) -> List[Dict[str, Any]]:
        """List all configured clients."""
        with self.lock:
            return [
                {
                    'type': info.get('type'),
                    'connected': not info.get('closed', True),
                    'connected_at': info.get('connected_at'),
                    'error': info.get('error')
                }
                for info in self.clients.values()
            ]

    def list_all_threads(self) -> List[Dict[str, Any]]:
        """List threads across all connected clients."""
        with self.lock:
            all_threads = []
            for client_type, info in self.clients.items():
                if not info.get('closed'):
                    threads = info.get('threads', [])
                    for t in threads:
                        t['client'] = client_type
                    all_threads.extend(threads)
            return all_threads

    def sync_threads(self, limit: int = 20):
        """Sync thread list from all connected clients."""
        with self.lock:
            for client_type, info in self.clients.items():
                if info.get('closed'):
                    continue

                rpc = info.get('rpc')
                if not rpc:
                    continue

                try:
                    data = rpc.call('thread/list', {'limit': limit})
                    threads = data.get('data', data.get('threads', []))
                    info['threads'] = [
                        {
                            'id': t.get('id'),
                            'title': t.get('name') or t.get('title') or f'任务 · {t.get("id")[:8]}',
                            'status': t.get('status', {'type': 'unknown'}),
                            'updatedAt': t.get('updatedAt'),
                            'client': client_type
                        }
                        for t in threads
                    ]
                except Exception as e:
                    info['threads'] = []
                    info['error'] = str(e)

    def get_quota(self, client_type: str = 'codex') -> Optional[Dict[str, Any]]:
        """Get quota info for a client."""
        with self.lock:
            return self.clients.get(client_type, {}).get('quota')

    def update_quota(self, client_type: str, quota_data: Dict[str, Any]):
        """Update quota for a client."""
        with self.lock:
            if client_type in self.clients:
                self.clients[client_type]['quota'] = quota_data
