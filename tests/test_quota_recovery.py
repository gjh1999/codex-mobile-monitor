"""Codex quota wait/resume with real scheduler persistence and local stop guards."""
import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from automation import Approvals, Scheduler
from quota_recovery import quota_state
from rpc_client import RPC, RPCError
from task_control import TaskControl

TID = '00000000-0000-4000-8000-000000000011'


def quota(allowed=False, used=100, reset=1000, secondary=20, second_reset=2000):
    return {'accountId': 'test-account', 'ordinaryUsageAllowed': allowed,
            'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': used, 'resetsAt': reset},
                           'secondary': {'usedPercent': secondary, 'resetsAt': second_reset},
                           'spendControlReached': False, 'rateLimitReachedType': None}}


class Peer:
    client_type = 'codex'
    closed = False

    def __init__(self):
        self.owned_threads = {TID}
        self.goal = {'status': 'usageLimited', 'objective': 'Continue the original authorized work',
                     'tokenBudget': 10000, 'tokensUsed': 500, 'updatedAt': 1}
        self.status = 'idle'
        self.flags = []
        self.calls = []
        self.sent = []
        self.hook = None
        self.uncertain = False
        self.auto_started = False

    def call(self, method, params):
        self.calls.append((method, params.copy()))
        if self.hook:
            self.hook(method, params)
        if method in ('thread/read', 'thread/resume'):
            return {'thread': {'id': TID, 'status': {'type': self.status, 'activeFlags': self.flags}, 'updatedAt': 1}}
        if method == 'thread/goal/get':
            return {'goal': copy.deepcopy(self.goal)}
        guard = params.get('_sendGuard')
        if guard is not None and not guard():
            raise RPCError(detail='Cancelled by local pause')
        if method == 'thread/goal/set':
            self.goal['status'] = params['status']
            if self.auto_started:
                self.status = 'active'
            return {'goal': copy.deepcopy(self.goal)}
        if method == 'turn/start':
            self.sent.append(params.copy())
            if self.uncertain:
                raise TimeoutError('No transport receipt')
            return {'turn': {'id': 'continued-once'}}
        if method == 'thread/turns/list':
            return {'data': []}
        return {}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scheduler = Scheduler(str(self.root / 'scheduler.db'))
        self.rpc = Peer()
        self.task = {'threadId': TID, 'enabled': True, 'exclusiveOwner': True, 'interval': 3600,
                     'prompt': 'Continue original work, preserving existing results.'}

    def tearDown(self):
        self.scheduler.db.close()
        self.temporary.cleanup()

    def tick(self, now, data=None, tasks=None):
        return self.scheduler.tick(self.rpc, [self.task] if tasks is None else tasks, now,
                                   quota=quota() if data is None else data)

    def restart(self):
        self.scheduler.db.close()
        self.scheduler = Scheduler(str(self.root / 'scheduler.db'))

    def test_exhaustion_waits_until_reset_then_confirms_and_resumes_once(self):
        before = copy.deepcopy(self.rpc.goal)
        self.assertEqual(self.tick(100), ['blocked:quota'])
        wait = self.scheduler.quota_recovery.snapshot()[TID]
        self.assertEqual(wait['resetsAt'], 1000)
        self.assertEqual(wait['due'], 1002)
        self.assertFalse(self.rpc.sent)
        self.assertEqual(self.tick(999, quota(True, 0)), ['blocked:quota'])
        self.assertEqual(self.tick(1002, quota(False, 100)), ['blocked:quota'])
        # Reset time alone never clears an exhausted quota, even if it is old.
        self.assertFalse(self.rpc.sent)
        self.assertEqual(self.tick(1062, quota(True, 0)), ['sent:quota'])
        self.assertEqual(len(self.rpc.sent), 1)
        self.assertEqual(self.rpc.goal['status'], 'active')
        self.assertEqual({k: v for k, v in self.rpc.goal.items() if k != 'status'},
                         {k: v for k, v in before.items() if k != 'status'})
        activation = next(p for m, p in self.rpc.calls if m == 'thread/goal/set')
        self.assertEqual(activation, {'threadId': TID, 'status': 'active', 'origin': 'automatic'})
        self.assertEqual(self.rpc.sent[0]['threadId'], TID)
        self.assertEqual(self.tick(1063, quota(True, 0)), [])
        self.restart()
        self.assertEqual(self.tick(4663, quota(True, 0)), ['blocked:duplicate'])
        self.assertEqual(len(self.rpc.sent), 1)

    def test_both_exhausted_windows_wait_for_the_later_reset(self):
        self.assertEqual(self.tick(100, quota(False, 100, 1000, 100, 2000)), ['blocked:quota'])
        self.assertEqual(self.scheduler.quota_recovery.snapshot()[TID]['due'], 2002)
        self.assertEqual(self.tick(1002, quota(False, 0, 1000, 100, 2000)), ['blocked:quota'])
        self.assertFalse(self.rpc.sent)
        self.assertEqual(self.tick(2002, quota(True, 0, 3000, 0, 4000)), ['sent:quota'])

    def test_wait_survives_restart_and_unknown_quota_does_not_resume(self):
        self.tick(100)
        self.restart()
        self.assertEqual(self.scheduler.quota_recovery.snapshot()[TID]['resetsAt'], 1000)
        self.assertEqual(self.tick(1002, {}), ['blocked:quota_unknown'])
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])
        self.assertEqual(self.tick(1062, quota(True, 0)), ['sent:quota'])

    def test_percentages_without_backend_recovery_permission_are_unknown(self):
        snapshot = quota(True, 0)
        snapshot.pop('ordinaryUsageAllowed')
        self.assertEqual(self.tick(100, snapshot), ['blocked:quota_unknown'])
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])

    def test_missing_reset_time_is_unknown_but_fresh_backend_recovery_is_enough(self):
        self.assertEqual(self.tick(100, quota(False, 100, None)), ['blocked:quota'])
        self.assertIsNone(self.scheduler.quota_recovery.snapshot()[TID]['resetsAt'])
        self.assertEqual(self.scheduler.quota_recovery.snapshot()[TID]['due'], 160)
        self.assertEqual(self.tick(160, quota(True, 0)), ['sent:quota'])

    def test_completed_paused_and_budget_limited_goals_are_never_reactivated(self):
        for status in ('complete', 'paused', 'budgetLimited', 'blocked'):
            with self.subTest(status=status):
                self.rpc.goal['status'] = status
                self.scheduler.db.execute('DELETE FROM tasks');self.scheduler.db.commit()
                self.assertEqual(self.tick(100, quota(True, 0)), ['blocked:goal'])
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])

    def test_usage_limited_goal_with_exhausted_task_budget_is_not_reactivated(self):
        self.rpc.goal.update(tokenBudget=500, tokensUsed=500)
        self.assertEqual(self.tick(100, quota(True, 0)), ['blocked:goal'])
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])

    def test_changed_goal_and_changed_account_need_manual_review(self):
        self.tick(100)
        self.rpc.goal['objective'] = 'a different task'
        self.assertEqual(self.tick(1002, quota(True, 0)), ['blocked:quota_changed'])
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])
        self.scheduler.db.execute('DELETE FROM quota_waits');self.scheduler.db.execute('DELETE FROM tasks');self.scheduler.db.commit()
        self.tick(100)
        changed_account = quota(True, 0)
        changed_account['accountId'] = 'different-account'
        self.assertEqual(self.tick(1002, changed_account), ['blocked:quota_account_changed'])
        self.assertFalse(self.rpc.sent)

    def test_sparse_failed_quota_read_does_not_look_like_an_account_change(self):
        self.tick(100)
        self.assertEqual(self.tick(1002, {}), ['blocked:quota_unknown'])
        self.assertEqual(self.scheduler.quota_recovery.snapshot()[TID]['state'], 'checking')
        self.assertEqual(self.tick(1062, quota(True, 0)), ['sent:quota'])

    def test_approvals_and_busy_turn_delay_recovery(self):
        approvals = Approvals()
        self.scheduler.approvals = approvals
        token = approvals.add(self.rpc, {'id': 1, 'method': 'item/commandExecution/requestApproval',
                                        'params': {'threadId': TID, 'command': 'git status'}})
        self.assertEqual(self.tick(100, quota(True, 0)), ['blocked:quota_approval'])
        approvals.resolve(self.rpc, 1)
        self.rpc.status = 'active'
        self.assertEqual(self.tick(160, quota(True, 0)), ['blocked:quota_busy'])
        self.rpc.status = 'idle'
        self.assertEqual(self.tick(220, quota(True, 0)), ['sent:quota'])

    def test_already_resumed_goal_finishes_wait_without_a_later_extra_prompt(self):
        self.assertEqual(self.tick(100), ['blocked:quota'])
        self.rpc.goal['status'] = 'active'
        self.rpc.status = 'active'
        self.assertEqual(self.tick(1002, quota(True, 0)), ['recovered:quota'])
        self.assertEqual(self.scheduler.quota_recovery.snapshot(), {})
        self.assertFalse(self.rpc.sent)
        self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])
        # When the native turn ends, the old quota wait must not send again.
        self.rpc.status = 'idle'
        self.assertEqual(self.tick(1062, quota(True, 0)), [])
        self.assertFalse(self.rpc.sent)
        self.restart()
        self.assertEqual(self.tick(4663, quota(True, 0)), ['blocked:duplicate'])
        self.assertFalse(self.rpc.sent)

    def test_goal_auto_start_does_not_send_an_additional_turn(self):
        self.rpc.auto_started = True
        self.assertEqual(self.tick(100, quota(True, 0)), ['recovered:quota'])
        self.assertFalse(self.rpc.sent)
        self.assertEqual(self.rpc.status, 'active')

    def test_uncertain_recovery_and_crash_reservation_are_not_replayed(self):
        self.rpc.uncertain = True
        self.assertEqual(self.tick(100, quota(True, 0)), ['blocked:quota_delivery'])
        self.assertEqual(len(self.rpc.sent), 1)
        self.restart()
        self.assertEqual(self.tick(10000, quota(True, 0)), ['blocked:quota_unknown'])
        self.assertEqual(len(self.rpc.sent), 1)
        self.scheduler.db.execute("UPDATE quota_waits SET state='reserved'");self.scheduler.db.commit()
        self.restart()
        self.assertEqual(self.scheduler.quota_recovery.snapshot()[TID]['state'], 'unknown')
        self.assertEqual(self.tick(20000, quota(True, 0)), ['blocked:quota_unknown'])
        self.assertEqual(len(self.rpc.sent), 1)

    def test_manual_pause_unmanage_detach_override_configuration_and_persist(self):
        for action in ('pause', 'unmanage', 'detach'):
            with self.subTest(action=action):
                control = TaskControl(self.root / (action + '.db'))
                try:
                    self.scheduler.db.execute('DELETE FROM quota_waits');self.scheduler.db.execute('DELETE FROM tasks');self.scheduler.db.commit()
                    self.rpc.goal['status'] = 'usageLimited'
                    control.seed([TID]);control.tick(self.rpc)
                    configured = self.task.copy()
                    tasks = control.automatic_tasks(self.rpc, [configured, *control.scheduled_tasks(self.rpc)])
                    self.assertEqual(len(tasks), 1)
                    self.assertEqual(self.tick(100, tasks=tasks), ['blocked:quota'])
                    control.submit(TID, action, request_id='stop-' + action)
                    control.tick(self.rpc)
                    tasks = control.automatic_tasks(self.rpc, [configured, *control.scheduled_tasks(self.rpc)])
                    self.assertEqual(tasks, [])
                    self.assertEqual(self.tick(1002, quota(True, 0), tasks), [])
                    self.assertEqual(self.scheduler.quota_recovery.snapshot(), {})
                    control.db.close()
                    control = TaskControl(self.root / (action + '.db'))
                    control.tick(self.rpc)
                    self.assertEqual(control.automatic_tasks(self.rpc, [configured]), [])
                finally:
                    control.db.close()
        self.assertFalse(self.rpc.sent)

    def test_pause_while_reading_prevents_goal_mutation_and_send(self):
        control = TaskControl(self.root / 'race.db')
        try:
            control.seed([TID]);control.tick(self.rpc)
            tasks = control.automatic_tasks(self.rpc, control.scheduled_tasks(self.rpc))
            self.assertEqual(self.tick(100, tasks=tasks), ['blocked:quota'])
            def pause(method, params):
                if method == 'thread/goal/get':
                    control.submit(TID, 'pause', request_id='pause-during-quota-read')
            self.rpc.hook = pause
            self.assertEqual(self.tick(1002, quota(True, 0), tasks), ['blocked:quota_cancelled'])
            self.assertFalse(self.rpc.sent)
            self.assertNotIn('thread/goal/set', [m for m, p in self.rpc.calls])
        finally:
            control.db.close()

    def test_rpc_guard_is_local_and_checked_under_the_write_lock(self):
        class Process:
            stdin = io.StringIO()
        rpc = RPC.__new__(RPC)
        rpc.p = Process();rpc.closed = False;rpc.seq = 0
        rpc.lock = threading.Lock();rpc.write_lock = threading.Lock();rpc.pending = {}
        with self.assertRaises(RPCError):
            rpc.call('turn/start', {'threadId': TID, '_sendGuard': lambda: False}, timeout=.1)
        self.assertEqual(rpc.p.stdin.getvalue(), '')
        self.assertEqual(rpc.pending, {})
        rpc.send({'method': 'test', 'params': {}}, guard=lambda: True)
        self.assertEqual(json.loads(rpc.p.stdin.getvalue())['method'], 'test')


class QuotaParsingTests(unittest.TestCase):
    def test_spend_control_and_missing_data_do_not_imply_recovery(self):
        data = quota(True, 0)
        data['rateLimits']['spendControlReached'] = True
        self.assertFalse(quota_state(data)['available'])
        self.assertIsNone(quota_state(data)['resetsAt'])
        data['rateLimits']['individualLimit'] = {'remainingPercent': 0, 'resetsAt': 3000}
        self.assertEqual(quota_state(data)['resetsAt'], 3000)
        self.assertFalse(quota_state({})['available'])
        data['rateLimits']['individualLimit'] = {'remainingPercent': None}
        self.assertFalse(quota_state(data)['available'])

    def test_credit_exhaustion_is_not_confused_with_normal_included_usage(self):
        data = quota(True, 0)
        data['rateLimits']['credits'] = {'hasCredits': False, 'unlimited': False, 'balance': '0'}
        self.assertTrue(quota_state(data)['available'])
        data['rateLimits']['rateLimitReachedType'] = 'workspace_owner_credits_depleted'
        self.assertFalse(quota_state(data)['available'])
        self.assertIsNone(quota_state(data)['resetsAt'])


if __name__ == '__main__':
    unittest.main()
