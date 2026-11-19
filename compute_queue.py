"""Persist and route fixed-snapshot compute jobs through the coordinator."""
import json
import re
from secrets import compare_digest, token_urlsafe

from coordination import Conflict


NAME = re.compile(r'[A-Za-z0-9_-]{1,80}')
WINDOW = 30


def validate_config(config, devices):
    for project, policy in config.items():
        if not NAME.fullmatch(project) or set(policy) != {'workers', 'profiles'}:
            raise ValueError('计算项目配置无效')
        for device, role in policy['workers'].items():
            if role not in ('home', 'local') or device not in devices or project not in devices[device]['projects']:
                raise ValueError('计算设备需登记所属项目及角色')
        for name, limits in policy['profiles'].items():
            if not NAME.fullmatch(name) or set(limits) != {'cpu', 'memoryMiB', 'vramMiB'} or any(
                    type(limits[key]) is not int or not low <= limits[key] <= high
                    for key, low, high in [('cpu', 1, 1024), ('memoryMiB', 1, 16 * 1024**2), ('vramMiB', 0, 1024**2)]):
                raise ValueError('项目运行配置或资源需求无效')


class ComputeQueue:
    def __init__(self, ownership, config):
        self.ownership = ownership
        self.config = config
        with ownership.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS compute_workers (
                    project TEXT NOT NULL, device TEXT NOT NULL, instance TEXT NOT NULL,
                    stamp REAL NOT NULL, capabilities TEXT NOT NULL, PRIMARY KEY(project, device)
                );
                CREATE TABLE IF NOT EXISTS compute_jobs (
                    seq INTEGER PRIMARY KEY, project TEXT NOT NULL, id TEXT NOT NULL,
                    submitter TEXT NOT NULL, session TEXT NOT NULL, profile TEXT NOT NULL, commit_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                    worker TEXT, instance TEXT, ticket TEXT, deadline REAL, started REAL, finished REAL,
                    exit_code INTEGER, result_sha256 TEXT, route TEXT,
                    UNIQUE(project, id)
                );
                CREATE INDEX IF NOT EXISTS compute_pending ON compute_jobs(project, state, seq);
                CREATE INDEX IF NOT EXISTS compute_routing ON compute_jobs(project,state,profile,commit_id,submitter,seq);
                CREATE INDEX IF NOT EXISTS compute_expiry ON compute_jobs(state, deadline, project);
                CREATE UNIQUE INDEX IF NOT EXISTS compute_worker_slot ON compute_jobs(worker)
                    WHERE state IN ('claimed', 'running', 'uncertain');
            ''')

    @staticmethod
    def view(row):
        return dict(id=row['id'], submitter=row['submitter'], profile=row['profile'], commit=row['commit_id'],
                    status=row['state'], createdAt=row['created'], attempt=row['attempt'], worker=row['worker'],
                    route=row['route'], startedAt=row['started'], finishedAt=row['finished'],
                    exitCode=row['exit_code'], resultSha256=row['result_sha256'])

    @staticmethod
    def find(db, project, job):
        return db.execute('SELECT * FROM compute_jobs WHERE project=? AND id=?', (project, job)).fetchone()

    @staticmethod
    def expire(db, now, project=None):
        scope = ' AND project=?' if project is not None else ''
        params = (now, project) if project is not None else (now,)
        db.execute("""UPDATE compute_jobs SET state='queued',worker=NULL,instance=NULL,ticket=NULL,deadline=NULL,route=NULL
                      WHERE state='claimed' AND deadline<=?""" + scope, params)
        db.execute("UPDATE compute_jobs SET state='uncertain' WHERE state='running' AND deadline<=?" + scope, params)

    def submit(self, project, device, job, session, profile, commit):
        with self.ownership.transaction() as db:
            now = self.ownership.clock()
            old = self.find(db, project, job)
            if old:
                if (old['submitter'], old['session'], old['profile'], old['commit_id']) != (device, session, profile, commit):
                    raise Conflict('作业编号已由其他请求使用')
                self.expire(db, now, project)
                return self.view(self.find(db, project, job))
            if profile not in self.config[project]['profiles']:
                raise Conflict('项目未登记此运行配置')
            active = db.execute('''SELECT 1 FROM activity WHERE project=? AND device=? AND session=?
                                   AND event!='close' AND source!='switch' AND stamp>?''',
                                (project, device, session, now - WINDOW)).fetchone()
            if not active:
                raise Conflict('提交计算作业需要本设备仍在线的前台会话')
            db.execute('INSERT INTO compute_jobs(project,id,submitter,session,profile,commit_id,created) VALUES (?,?,?,?,?,?,?)',
                       (project, job, device, session, profile, commit, now))
            return self.view(self.find(db, project, job))

    def status(self, project, device, job):
        with self.ownership.transaction() as db:
            self.expire(db, self.ownership.clock(), project)
            row = self.find(db, project, job)
            if not row or device not in (row['submitter'], row['worker']):
                return None
            return self.view(row)

    def heartbeat(self, project, device, instance, capabilities):
        if not set(capabilities['profiles']) <= self.config[project]['profiles'].keys():
            raise Conflict('设备报告了未登记的运行配置')
        with self.ownership.transaction() as db:
            db.execute('''INSERT INTO compute_workers VALUES (?,?,?,?,?)
                          ON CONFLICT(project,device) DO UPDATE SET instance=excluded.instance,
                          stamp=excluded.stamp,capabilities=excluded.capabilities''',
                       (project, device, instance, self.ownership.clock(), json.dumps(capabilities)))

    @staticmethod
    def eligible(profile, limits, capabilities):
        return (profile in capabilities['profiles'] and capabilities['cpu'] >= limits['cpu']
                and capabilities['memoryMiB'] >= limits['memoryMiB']
                and max(capabilities['gpuVramMiB'], default=0) >= limits['vramMiB'])

    @staticmethod
    def route(devices, submitter, gpu):
        home = sorted(name for name, role in devices.items() if role == 'home')
        if gpu and home:
            return home[0], 'home_gpu'
        if submitter in devices:
            return submitter, 'local_capacity'
        if home:
            return home[0], 'home_capacity'
        return None, None

    def claim(self, project, device, instance):
        policy = self.config[project]
        with self.ownership.transaction() as db:
            now = self.ownership.clock()
            self.expire(db, now)
            workers = {row['device']: row for row in db.execute(
                'SELECT * FROM compute_workers WHERE project=? AND stamp>?', (project, now - WINDOW))
                if row['device'] in policy['workers']}
            if device not in workers or workers[device]['instance'] != instance:
                raise Conflict('计算端需要先发送当前实例的心跳')
            active = {row['worker']: row for row in db.execute(
                "SELECT * FROM compute_jobs WHERE state IN ('claimed','running','uncertain')")}
            current = active.get(device)
            if current:
                if (current['project'], current['instance']) != (project, instance):
                    return None
                return dict(self.view(current), ticket=current['ticket'])

            ready = {name: json.loads(row['capabilities']) for name, row in workers.items() if name not in active}
            mine = ready[device]
            profiles = tuple(name for name, limits in policy['profiles'].items() if self.eligible(name, limits, mine))
            revisions = tuple(dict.fromkeys(mine['commits']))
            if not profiles or not revisions:
                return None
            # Route decisions are identical within one submitter/profile/commit group.
            # Let SQLite find its oldest row without loading that backlog into Python.
            jobs = db.execute(f"""SELECT MIN(seq) AS seq,submitter,profile,commit_id
                                   FROM compute_jobs WHERE project=? AND state='queued'
                                   AND profile IN ({','.join('?' for _ in profiles)})
                                   AND commit_id IN ({','.join('?' for _ in revisions)})
                                   GROUP BY submitter,profile,commit_id ORDER BY seq""",
                              (project, *profiles, *revisions))
            candidates = {}
            for row in jobs:
                profile, commit = row['profile'], row['commit_id']
                limits = policy['profiles'][profile]
                key = profile, commit
                if key not in candidates:
                    candidates[key] = {name: policy['workers'][name] for name, capabilities in ready.items()
                                       if commit in capabilities['commits'] and self.eligible(profile, limits, capabilities)}
                chosen, reason = self.route(candidates[key], row['submitter'], limits['vramMiB'] > 0)
                if chosen == device:
                    ticket = token_urlsafe(32)
                    db.execute("""UPDATE compute_jobs SET state='claimed',worker=?,instance=?,ticket=?,deadline=?,
                                  route=?,attempt=attempt+1 WHERE seq=?""",
                               (device, instance, ticket, now + WINDOW, reason, row['seq']))
                    claimed = db.execute('SELECT * FROM compute_jobs WHERE seq=?', (row['seq'],)).fetchone()
                    return dict(self.view(claimed), ticket=ticket)
            return None

    def update(self, project, device, job, instance, ticket, event, exit_code=None, result=None):
        with self.ownership.transaction() as db:
            now = self.ownership.clock()
            self.expire(db, now, project)
            row = self.find(db, project, job)
            if (not row or (row['worker'], row['instance']) != (device, instance)
                    or not compare_digest(row['ticket'] or '', ticket)):
                raise Conflict('计算作业凭据无效或已过期')
            status = row['state']
            if event == 'start' and status == 'claimed':
                db.execute("UPDATE compute_jobs SET state='running',started=?,deadline=? WHERE seq=?",
                           (now, now + WINDOW, row['seq']))
            elif event == 'start' and status == 'running':
                return self.view(row)
            elif event == 'renew' and status == 'running':
                db.execute('UPDATE compute_jobs SET deadline=? WHERE seq=?', (now + WINDOW, row['seq']))
            elif event == 'finish' and status in ('running', 'uncertain'):
                db.execute("""UPDATE compute_jobs SET state='finished',finished=?,exit_code=?,result_sha256=?
                              WHERE seq=?""", (now, exit_code, result, row['seq']))
            elif event == 'finish' and status == 'finished' and (row['exit_code'], row['result_sha256']) == (exit_code, result):
                return self.view(row)
            else:
                raise Conflict('计算作业已离开所需阶段')
            return self.view(self.find(db, project, job))

    def confirm_stopped(self, project, job, attempt, worker):
        """Release an uncertain slot after the coordinator operator checks the worker."""
        with self.ownership.transaction() as db:
            now = self.ownership.clock()
            self.expire(db, now, project)
            row = self.find(db, project, job)
            if not row or (row['state'], row['attempt'], row['worker']) != ('uncertain', attempt, worker):
                raise Conflict('计算作业状态、尝试编号或执行设备已变化')
            db.execute("UPDATE compute_jobs SET state='abandoned',finished=?,ticket=NULL WHERE seq=?", (now, row['seq']))
            return self.view(self.find(db, project, job))
