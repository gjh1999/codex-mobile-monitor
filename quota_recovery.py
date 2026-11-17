"""Persistent Codex quota waits; resume only on fresh, explicit backend recovery."""
import hashlib
import json
import math
import uuid


def quota_state(snapshot):
    """Return availability and the latest reset among actually exhausted windows.

    Reset timestamps predict when to query again; they never prove recovery.
    ordinaryUsageAllowed is the server's account-validated recovery signal.
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    buckets = snapshot.get('rateLimitsByLimitId') or {}
    bucket = buckets.get('codex') or snapshot.get('rateLimits') or {}
    blocked = snapshot.get('ordinaryUsageAllowed') is False
    resets = []
    missing_reset = False
    for key in ('primary', 'secondary'):
        window = bucket.get(key) or {}
        used = window.get('usedPercent')
        if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= 100:
            blocked = True
            reset = window.get('resetsAt')
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) and reset > 0:
                resets.append(float(reset))
            else:
                missing_reset = True
    individual = bucket.get('individualLimit') or {}
    remaining = individual.get('remainingPercent')
    if bucket.get('spendControlReached') is True or (isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0):
        blocked = True
        reset = individual.get('resetsAt')
        if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) and reset > 0:
            resets.append(float(reset))
        else:
            missing_reset = True
    reached = bucket.get('rateLimitReachedType')
    if reached:
        blocked = True
        if reached != 'rate_limit_reached':
            missing_reset = True
    reset_at = max(resets) if resets and not missing_reset else None
    available = snapshot.get('ordinaryUsageAllowed') is True and not blocked
    return {'available': available, 'blocked': blocked, 'resetsAt': reset_at,
            'account': snapshot.get('accountId')}


def _goal_key(goal):
    return hashlib.sha256(json.dumps([goal.get('objective'), goal.get('tokenBudget')],
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class QuotaRecovery:
    def __init__(self, db):
        self.db = db
        self.db.execute('CREATE TABLE IF NOT EXISTS quota_waits(thread TEXT PRIMARY KEY, due REAL NOT NULL, resets REAL, state TEXT NOT NULL, goal TEXT NOT NULL, account TEXT, reservation TEXT NOT NULL)')
        # A crash between activating a goal and confirming turn/start is uncertain.
        # Do not automatically activate/send a second time after a restart.
        self.db.execute("UPDATE quota_waits SET state='unknown' WHERE state='reserved'")
        self.db.commit()

    def get(self, tid):
        row = self.db.execute('SELECT due,resets,state,goal,account,reservation FROM quota_waits WHERE thread=?', (tid,)).fetchone()
        if not row or row[2] in ('done', 'cancelled'):
            return None
        return dict(zip(('due', 'resetsAt', 'state', 'goal', 'account', 'reservation'), row))

    def reconcile(self, tasks):
        eligible = {t['threadId'] for t in tasks if t.get('enabled') and t.get('exclusiveOwner') and not t.get('completed')}
        for (tid,) in self.db.execute("SELECT thread FROM quota_waits WHERE state IN ('waiting','checking')").fetchall():
            if tid not in eligible:
                self.cancel(tid)

    def cancel(self, tid):
        self.db.execute("UPDATE quota_waits SET state='cancelled' WHERE thread=? AND state!='unknown'", (tid,))
        self.db.commit()

    @staticmethod
    def allowed(rpc, task):
        guard = task.get('_sendGuard')
        return (task.get('enabled') and task.get('exclusiveOwner') and not task.get('completed') and
                not getattr(rpc, 'closed', False) and task['threadId'] in getattr(rpc, 'owned_threads', set()) and
                (guard is None or guard()))

    def defer(self, tid, state, now, reset=None):
        due = reset + 2 if reset is not None and reset + 2 > now else now + 60
        self.db.execute('UPDATE quota_waits SET state=?,due=?,resets=? WHERE thread=?', (state, due, reset, tid))
        self.db.commit()

    def handle(self, rpc, task, thread, goal, snapshot, now, approvals=None):
        tid = task['threadId']
        current = self.get(tid)
        quota = quota_state(snapshot)
        if current and current['state'] in ('unknown', 'changed', 'account_changed'):
            return 'blocked:quota_' + current['state']
        if not self.allowed(rpc, task):
            self.cancel(tid)
            return 'blocked:quota_cancelled'
        status = goal.get('status') if goal else None
        if status not in ('active', 'usageLimited') or not goal.get('objective'):
            if current:
                self.cancel(tid)
            return 'blocked:goal'
        budget, used = goal.get('tokenBudget'), goal.get('tokensUsed')
        if isinstance(budget, (int, float)) and isinstance(used, (int, float)) and used >= budget:
            if current:
                self.cancel(tid)
            return 'blocked:goal'
        key = _goal_key(goal)
        account = str(quota['account']) if quota['account'] is not None else None
        if current and current['goal'] != key:
            self.db.execute("UPDATE quota_waits SET state='changed' WHERE thread=?", (tid,));self.db.commit()
            return 'blocked:quota_changed'
        if current and current['account'] is not None:
            if account is None:
                self.defer(tid, 'checking', now)
                return 'blocked:quota_unknown'
            if current['account'] != account:
                self.db.execute("UPDATE quota_waits SET state='account_changed' WHERE thread=?", (tid,));self.db.commit()
                return 'blocked:quota_account_changed'
        elif current and account is not None:
            self.db.execute('UPDATE quota_waits SET account=? WHERE thread=?', (account, tid));self.db.commit()
        if current is None:
            self.db.execute("INSERT OR REPLACE INTO quota_waits VALUES(?,?,?,'waiting',?,?,?)",
                            (tid, now, quota['resetsAt'], key, account, 'quota-' + uuid.uuid4().hex))
            self.db.commit()
            current = self.get(tid)
        if not quota['available']:
            self.defer(tid, 'waiting' if quota['blocked'] else 'checking', now, quota['resetsAt'])
            return 'blocked:quota' if quota['blocked'] else 'blocked:quota_unknown'
        if current['due'] > now:
            return 'blocked:quota'
        live_status = thread.get('status') or {}
        pending_approval = approvals and approvals.pending_for(tid)
        if status == 'active' and live_status.get('type') == 'active' and not live_status.get('activeFlags') and not pending_approval:
            # The native goal may already have resumed while we waited. Finish
            # the wait now so its next idle state does not trigger an extra prompt.
            self.db.execute("UPDATE quota_waits SET state='done' WHERE thread=?", (tid,));self.db.commit()
            return 'recovered:quota'
        if live_status.get('type') != 'idle' or live_status.get('activeFlags'):
            self.defer(tid, 'checking', now)
            return 'blocked:quota_busy'
        if pending_approval:
            self.defer(tid, 'checking', now)
            return 'blocked:quota_approval'
        # Recheck both ownership/local stop intent and the goal immediately before
        # mutation. Never overwrite an edited objective or a budget/completed goal.
        latest = rpc.call('thread/goal/get', {'threadId': tid}).get('goal') or {}
        budget, used = latest.get('tokenBudget'), latest.get('tokensUsed')
        exhausted_budget = isinstance(budget, (int, float)) and isinstance(used, (int, float)) and used >= budget
        if latest.get('status') not in ('active', 'usageLimited') or _goal_key(latest) != key or exhausted_budget:
            self.db.execute("UPDATE quota_waits SET state='changed' WHERE thread=?", (tid,));self.db.commit()
            return 'blocked:quota_changed'
        if not self.allowed(rpc, task):
            self.cancel(tid)
            return 'blocked:quota_cancelled'
        self.db.execute("UPDATE quota_waits SET state='reserved' WHERE thread=?", (tid,));self.db.commit()
        try:
            if latest.get('status') == 'usageLimited':
                # Change status only: leave objective, tokensUsed and tokenBudget
                # intact, and label this as an automatic lifecycle transition.
                params = {'threadId': tid, 'status': 'active', 'origin': 'automatic'}
                if task.get('_sendGuard') is not None:
                    params['_sendGuard'] = task['_sendGuard']
                restored = rpc.call('thread/goal/set', params).get('goal') or {}
                if restored.get('status') != 'active' or _goal_key(restored) != key:
                    raise RuntimeError('Goal recovery was not confirmed')
            latest_thread = rpc.call('thread/read', {'threadId': tid, 'includeTurns': False}).get('thread') or {}
            live_status = latest_thread.get('status') or {}
            if not self.allowed(rpc, task):
                self.cancel(tid)
                return 'blocked:quota_cancelled'
            if live_status.get('type') == 'active':
                # Codex may start its own goal turn when its status changes.
                # An already running turn is recovery, not a reason to send again.
                self.db.execute("UPDATE quota_waits SET state='done' WHERE thread=?", (tid,));self.db.commit()
                return 'recovered:quota'
            if live_status.get('type') != 'idle' or live_status.get('activeFlags') or (approvals and approvals.pending_for(tid)):
                raise RuntimeError('Recovered goal has no confirmed idle turn')
            params = {'threadId': tid, 'input': [{'type': 'text', 'text': task['prompt']}],
                      'clientUserMessageId': current['reservation']}
            if task.get('_sendGuard') is not None:
                params['_sendGuard'] = task['_sendGuard']
            rpc.call('turn/start', params)
        except Exception:
            self.db.execute("UPDATE quota_waits SET state='unknown' WHERE thread=?", (tid,));self.db.commit()
            return 'blocked:quota_delivery'
        self.db.execute("UPDATE quota_waits SET state='done' WHERE thread=?", (tid,));self.db.commit()
        return 'sent:quota'

    def snapshot(self):
        result = {}
        for tid, due, reset, state in self.db.execute("SELECT thread,due,resets,state FROM quota_waits WHERE state NOT IN ('done','cancelled')"):
            message = {'waiting': '额度已用完，等待刷新后自动继续。',
                       'checking': '等待服务端确认额度恢复后自动继续。',
                       'reserved': '额度已恢复，正在继续原任务。',
                       'unknown': '额度恢复操作结果不确定，请手动核对任务；不会重复发送。',
                       'changed': '任务目标或预算已变化，已停止自动恢复。',
                       'account_changed': '额度所属账号已变化，已停止自动恢复。'}[state]
            result[tid] = {'state': state, 'due': due, 'resetsAt': reset, 'message': message}
        return result
