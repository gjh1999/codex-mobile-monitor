"""Group local conversations by workspace and index their existing titles."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path, PureWindowsPath


def folder_name(directory):
    return PureWindowsPath(directory).name if isinstance(directory, str) and directory else ''


@lru_cache(maxsize=256)
def project_group(directory):
    if not directory:
        return '', '未归档', ''
    path = Path(directory)
    workspace = directory
    if path.is_absolute():
        try:
            if path.is_dir():
                workspace = str(path.resolve())
                path = Path(workspace)
                workspace = next((str(parent) for parent in (path, *path.parents)
                                  if (parent / '.git').exists()), workspace)
        except OSError:
            pass
    normalized = os.path.normcase(os.path.normpath(workspace))
    return normalized, folder_name(workspace) or workspace, workspace


def usable_title(value, project):
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())
    if (not text or text.casefold() == project.casefold()
            or text.startswith(('历史任务 ·', '未命名', '# AGENTS.md', '<environment_context'))
            or text in ('继续', '接着做', '好的', '好', '嗯')):
        return ''
    return text


class SessionTitles:
    """Use Codex's local topic titles, with a bounded transcript fallback."""
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
        self.index_stamp = None
        self.names = {}
        self.transcripts = {}
        self.transcript_scan = False

    def refresh(self):
        path = self.root / 'session_index.jsonl'
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self.index_stamp:
                return
            with path.open(encoding='utf-8') as stream:
                names = {entry.get('id'): entry.get('thread_name') for entry in self.records(stream)}
        except OSError:
            return
        self.names = names
        self.index_stamp = stamp
        self.transcript_scan = False

    @staticmethod
    def records(lines):
        for line in lines:
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(entry, dict):
                yield entry

    @lru_cache(maxsize=128)
    def first_topic(self, path, project, _stamp):
        try:
            with path.open('rb') as stream:
                data = stream.read(384 * 1024)
        except OSError:
            return ''
        for entry in self.records(data.splitlines()):
            payload = entry.get('payload') or {}
            if not isinstance(payload, dict):
                continue
            if entry.get('type') != 'response_item' or payload.get('type') != 'message' or payload.get('role') != 'user':
                continue
            content = payload.get('content')
            if not isinstance(content, list):
                continue
            for item in content:
                raw = item.get('text') if isinstance(item, dict) else ''
                if not isinstance(raw, str) or raw.lstrip().startswith(('# AGENTS.md', '<environment_context', '<INSTRUCTIONS>')):
                    continue
                for line in raw.splitlines():
                    if re.search(r'(?i)(password\s*[:=]|token\s*[:=]|api[_ -]?key|密码\s*[:：]|https?://)', line):
                        continue
                    text = usable_title(line.strip(' #\t'), project)
                    if text:
                        return text if len(text) <= 36 else text[:35] + '…'
        return ''

    def title(self, thread_id, project, *candidates, codex=True):
        if codex:
            candidates = (self.names.get(thread_id), *candidates)
        for candidate in candidates:
            title = usable_title(candidate, project)
            if title:
                return title
        if codex and not self.transcript_scan:
            for base in (self.root / 'sessions', self.root / 'archived_sessions'):
                try:
                    self.transcripts.update((path.stem[-36:], path) for path in base.rglob('rollout-*.jsonl'))
                except OSError:
                    continue
            self.transcript_scan = True
        path = self.transcripts.get(thread_id) if codex else None
        if path:
            try:
                return self.first_topic(path, project, path.stat().st_mtime_ns)
            except OSError:
                pass
        return ''
