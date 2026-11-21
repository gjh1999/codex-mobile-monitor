import json
import tempfile
import unittest
from pathlib import Path

from desktop_projects import SessionTitles, project_group


class DesktopProjectsTests(unittest.TestCase):
    def test_subdirectories_group_under_git_root_and_same_names_stay_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'team-a' / 'codexDesk'
            second = root / 'team-b' / 'codexDesk'
            (first / 'src').mkdir(parents=True)
            (first / '.git').write_text('gitdir: worktree', encoding='utf-8')
            second.mkdir(parents=True)
            self.assertEqual(project_group(str(first)), project_group(str(first / 'src')))
            self.assertNotEqual(project_group(str(first))[0], project_group(str(second))[0])
            self.assertEqual(project_group(str(first))[1], 'codexDesk')

    def test_discovered_projects_and_unmatched_tasks_do_not_group_under_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first, second = root / 'first', root / 'second'
            (root / '.git').mkdir()
            (first / 'src').mkdir(parents=True)
            second.mkdir()
            known = frozenset({first})
            self.assertEqual(project_group(str(first / 'src'), known, root)[2], str(first))
            self.assertEqual(project_group(str(second), known, root)[2], str(second))

    def test_conversation_index_and_plain_user_topic_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            indexed = '11111111-1111-4111-8111-111111111111'
            unindexed = '22222222-2222-4222-8222-222222222222'
            (root / 'sessions').mkdir()
            (root / 'session_index.jsonl').write_text(
                'partial\n' + json.dumps({'id': indexed, 'thread_name': '整理海边的随记'}), encoding='utf-8')
            transcript = root / 'sessions' / f'rollout-test-{unindexed}.jsonl'
            texts = ['# AGENTS.md instructions', '继续', 'token=example', '核对奶油色打印样稿']
            transcript.write_text('\n'.join(json.dumps({
                'type': 'response_item',
                'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]},
            }) for text in texts), encoding='utf-8')
            titles = SessionTitles(root)
            titles.refresh()
            self.assertEqual(titles.title(indexed, 'codexDesk', '未命名历史任务'), '整理海边的随记')
            self.assertEqual(titles.title(unindexed, 'codexDesk', 'codexDesk'), '核对奶油色打印样稿')
