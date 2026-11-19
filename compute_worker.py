"""Run allowlisted Windows compute profiles and retain recoverable results locally."""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from compute_queue import NAME
from coordination_client import ControlDenied, device_client
from handoff import GIT_ID, file_hash, git, limited, local_path, require, sha
from handoff_merge import check_checkout_paths, object_store, separate, worktree_root
from handoff_sync import absolute, atomic_write, encode
from lfs_assets import ordinary
from service_lock import ServiceLock
from windows_job import WindowsJob

LOG_LIMIT = 2 * 1024**2
OUTPUT_LIMIT = 128 * 1024**2
POLL_SECONDS = 5
WORKER_SCRIPT = Path(__file__).resolve()


def record(path):
    return json.loads(limited(path, 2 * 1024**2).decode('utf-8-sig'))


def write_record(path, data):
    atomic_write(path, encode(data))


def bounded_number(value, low, high):
    return type(value) is int and low <= value <= high


def profile_config(config):
    command = config['command']
    require(isinstance(command, list) and command and all(isinstance(value, str) and '\0' not in value for value in command),
            '计算命令需要固定参数列表')
    program = absolute(command[0])
    require(program.suffix.lower() == '.exe' and program.is_file(), '计算命令需要本机绝对路径下的 Windows 可执行文件')
    outputs = config.get('outputs', [])
    require(isinstance(outputs, list) and len(outputs) <= 128 and all(isinstance(name, str) for name in outputs),
            '计算输出需要登记的相对文件列表')
    timeout = config.get('timeoutSeconds', 900)
    require(bounded_number(timeout, 1, 86400), '计算超时需在 1 到 86400 秒之间')
    environment = config.get('environment', {})
    require(isinstance(environment, dict) and all(isinstance(value, str) and '\0' not in value
            and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) and key.upper() != 'LANXI_DEVICE_TOKEN'
            for key, value in environment.items()), '计算命令的本地环境配置无效')
    return dict(command=[str(program), *command[1:]], outputs=list(dict.fromkeys(outputs)),
                timeoutSeconds=timeout, environment=environment)


def worker_projects(client, config, state_dir):
    result = {}
    for project, settings in config['projects'].items():
        root, = (path for path, name in client.paths.items() if name == project)
        repo = worktree_root(root)
        require(separate(repo, state_dir), '计算状态目录需要独立于项目仓库')
        commits = settings['commits']
        require(isinstance(commits, list) and 0 < len(commits) <= 64 and all(
            isinstance(commit, str) and GIT_ID.fullmatch(commit) for commit in commits), '需要本机明确批准的 Git 提交')
        for commit in commits:
            require(git(repo, 'rev-parse', '--verify', commit + '^{commit}', isolated=True).decode().strip() == commit,
                    '登记的计算提交不存在于本机')
        profiles = {name: profile_config(data) for name, data in settings['profiles'].items()}
        require(0 < len(profiles) <= 64 and all(NAME.fullmatch(name) for name in profiles), '计算运行配置名称无效')
        for profile in profiles.values():
            for name in profile['outputs']:
                local_path(state_dir, name)
        cpu, memory = settings['cpu'], settings['memoryMiB']
        cards = settings.get('gpuVramMiB', [])
        require(bounded_number(cpu, 0, 1024) and bounded_number(memory, 0, 16 * 1024**2)
                and isinstance(cards, list) and len(cards) <= 64
                and all(bounded_number(card, 0, 1024**2) for card in cards), '本机计算资源配置无效')
        result[project] = dict(repo=repo, profiles=profiles, commits=commits, cpu=cpu, memoryMiB=memory, gpuVramMiB=cards)
    require(result, '至少配置一个本机执行项目')
    return result


def snapshot(repo, commit, attempt):
    work = attempt / 'work'
    work.mkdir()
    with tempfile.TemporaryDirectory(dir=attempt) as directory:
        scratch = Path(directory)
        git(scratch, 'init', '-q', '--template=', '--object-format=' + ('sha256' if len(commit) == 64 else 'sha1'),
            isolated=True)
        (scratch / '.git/objects/info/alternates').write_bytes((object_store(repo) + '\n').encode())
        git(scratch, 'read-tree', commit + '^{tree}', isolated=True)
        check_checkout_paths(scratch)
        git(scratch, 'checkout-index', '--all', '--prefix=' + work.as_posix() + '/', isolated=True)
    return work


def safe_env(temp, overrides=None):
    variables = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'SystemDrive') if key in os.environ}
    system_root = Path(variables['SystemRoot'])
    variables.update(PATH=os.pathsep.join(map(str, (system_root / 'System32', system_root))),
                     TEMP=str(temp), TMP=str(temp))
    variables.update(overrides or {})
    return variables


def run_child(attempt):
    if sys.stdin.buffer.read(1) != b'1':
        return 125
    spec = record(attempt / 'launch.json')
    work = attempt / 'work'
    try:
        return subprocess.run(spec['profile']['command'], cwd=work, stdin=subprocess.DEVNULL, shell=False,
                              env=safe_env(attempt / 'tmp', spec['profile']['environment'])).returncode
    except OSError as error:
        print(f'无法启动本机计算命令：{error}', file=sys.stderr)
        return 125


def run_profile(attempt, timeout):
    """Release the launcher only after attaching its process tree to a Job Object."""
    job = WindowsJob()
    reader = None
    failure = []
    dropped = 0
    child = None
    assigned = False
    try:
        child = subprocess.Popen([sys.executable, '-E', '-s', str(WORKER_SCRIPT), '--execute', str(attempt)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=safe_env(attempt / 'tmp'), creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(child.pid)
        assigned = True

        def capture_log():
            nonlocal dropped
            try:
                written = 0
                with (attempt / 'process.log').open('wb') as log:
                    while block := child.stdout.read(65536):
                        count = min(len(block), LOG_LIMIT - written)
                        log.write(block[:count])
                        written += count
                        dropped += len(block) - count
                    log.flush()
                    os.fsync(log.fileno())
            except OSError as error:
                failure.append(error)

        reader = threading.Thread(target=capture_log, daemon=True)
        reader.start()
        child.stdin.write(b'1')
        child.stdin.close()
        try:
            code = child.wait(timeout=timeout)
            outcome = 'exited'
        except subprocess.TimeoutExpired:
            code, outcome = 124, 'timed_out'
    finally:
        try:
            if child is not None:
                if assigned:
                    # Also stop descendants still alive after the main command exits.
                    job.stop()
                elif child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        finally:
            job.close()
            if child is not None and not child.stdin.closed:
                child.stdin.close()
            if reader is not None:
                reader.join(5)
                if reader.is_alive():
                    raise TimeoutError('计算进程日志尚未收尾')
            if child is not None:
                child.stdout.close()
    if failure:
        raise failure[0]
    return code, outcome, dropped


def artifact(root, name, limit):
    path = local_path(root, name)
    info = ordinary(path)
    require(info and stat.S_ISREG(info.st_mode) and info.st_size <= limit, '计算产物缺失或大小超限')
    with path.open('rb') as stream:
        digest = file_hash(stream)
        end = os.fstat(stream.fileno())
    require((end.st_size, end.st_mtime_ns) == (info.st_size, info.st_mtime_ns), '计算产物校验时发生变化')
    return dict(path=name, bytes=info.st_size, sha256=digest)


def supervise(attempt):
    try:
        lock = ServiceLock(attempt / 'supervisor.lockfile')
    except RuntimeError:
        return
    try:
        started = attempt / 'entered.json'
        if started.exists():
            return
        spec = record(attempt / 'launch.json')
        write_record(started, {'pid': os.getpid()})
        manifest = {name: spec[name] for name in ('project', 'id', 'attempt', 'commit', 'name')}
        manifest.update(version=1, outputs=[], droppedLogBytes=0)
        try:
            work = snapshot(Path(spec['repo']), spec['commit'], attempt)
            (attempt / 'tmp').mkdir()
        except (OSError, ValueError) as error:
            (attempt / 'process.log').write_text(str(error)[:4096] + '\n', encoding='utf-8')
            manifest.update(exitCode=125, outcome='preparation_error')
        else:
            # An unexpected process-tree failure leaves the entered marker only.
            code, outcome, dropped = run_profile(attempt, spec['profile']['timeoutSeconds'])
            manifest.update(exitCode=code, outcome=outcome, droppedLogBytes=dropped)
            if code == 0:
                remaining = OUTPUT_LIMIT
                try:
                    for name in spec['profile']['outputs']:
                        item = artifact(work, name, remaining)
                        manifest['outputs'].append(item)
                        remaining -= item['bytes']
                except (OSError, ValueError) as error:
                    manifest.update(exitCode=125, outcome='output_error', outputs=[], error=str(error)[:4096])
        manifest['log'] = artifact(attempt, 'process.log', LOG_LIMIT)
        write_record(attempt / 'result.json', manifest)
    finally:
        lock.close()


def result_report(attempt, active):
    path = attempt / 'result.json'
    raw = limited(path, 2 * 1024**2)
    result = json.loads(raw)
    require(result['version'] == 1 and all(result[key] == active[key] for key in ('project', 'id', 'attempt', 'commit', 'name'))
            and bounded_number(result['exitCode'], -(2**31), 2**32 - 1), '计算回执与执行记录不一致')
    require(artifact(attempt, 'process.log', LOG_LIMIT) == result['log'], '计算日志校验失败')
    names = active['profile']['outputs'] if result['exitCode'] == 0 else []
    require([item['path'] for item in result['outputs']] == names, '计算产物与本地登记不一致')
    remaining = OUTPUT_LIMIT
    for item in result['outputs']:
        require(artifact(attempt / 'work', item['path'], remaining) == item, '计算产物校验失败')
        remaining -= item['bytes']
    return dict(exitCode=result['exitCode'], resultSha256=sha(raw))


def supervisor_alive(attempt):
    try:
        lock = ServiceLock(attempt / 'supervisor.lockfile')
    except RuntimeError:
        return True
    lock.close()
    return False


class Worker:
    def __init__(self, client, projects, state_dir):
        self.client, self.projects, self.state_dir = client, projects, state_dir
        self.active_file = state_dir / 'active.json'
        self.supervisor = None
        self.supervisor_env = safe_env(state_dir)
        git_executable = shutil.which('git')
        require(git_executable, '计算执行端需要已安装的 Git')
        self.supervisor_env['PATH'] = os.pathsep.join((str(Path(git_executable).resolve().parent), self.supervisor_env['PATH']))
        identity_file = state_dir / 'identity.json'
        binding = dict(device=client.device, url=client.url)
        if not identity_file.exists():
            write_record(identity_file, dict(binding, instance=str(uuid.uuid4())))
        identity = record(identity_file)
        require(all(identity[key] == value for key, value in binding.items()), '计算状态目录已登记给另一台设备或中心')
        self.instance = str(uuid.UUID(identity['instance']))

    def claim(self):
        for project, plan in self.projects.items():
            self.client.request(project, 'compute/heartbeat', dict(instance=self.instance, profiles=list(plan['profiles']),
                                **{key: plan[key] for key in ('commits', 'cpu', 'memoryMiB', 'gpuVramMiB')}))
            job = self.client.request(project, 'compute/claim', {'instance': self.instance})['job']
            if job is None:
                continue
            require(job['status'] == 'claimed' and job['worker'] == self.client.device
                    and job['commit'] in plan['commits'] and job['profile'] in plan['profiles']
                    and bounded_number(job['attempt'], 1, 2**53 - 1)
                    and re.fullmatch(r'[A-Za-z0-9_-]{43}', job['ticket']), '领取的计算作业与本机配置不符')
            active = dict(project=project, id=str(uuid.UUID(job['id'])), attempt=job['attempt'], ticket=job['ticket'],
                          commit=job['commit'], repo=str(plan['repo']), name=job['profile'],
                          profile=plan['profiles'][job['profile']], phase='claimed')
            write_record(self.active_file, active)
            return active
        return None

    def event(self, active, kind, **fields):
        return self.client.request(active['project'], f"compute/jobs/{active['id']}/events",
                                   dict(instance=self.instance, ticket=active['ticket'], event=kind, **fields))

    def check_conflict(self, active, attempt):
        try:
            remote = self.client.request(active['project'], f"compute/jobs/{active['id']}")
        except ControlDenied as error:
            if error.code != 404 or active['phase'] != 'claimed':
                raise
            remote = {'status': 'queued'}
        stale_claim = active['phase'] == 'claimed' and (
            remote['status'] == 'queued' or (remote['attempt'], remote['worker']) != (active['attempt'], self.client.device))
        if (remote['status'] == 'abandoned' or stale_claim) and not supervisor_alive(attempt):
            self.active_file.unlink()
            return 'released'
        return 'uncertain'

    def step(self):
        active = record(self.active_file) if self.active_file.exists() else self.claim()
        if active is None:
            return 'idle'
        attempt = local_path(self.state_dir, f"attempts/{active['project']}/{active['id']}/{active['attempt']}")
        attempt.mkdir(parents=True, exist_ok=True)
        try:
            if (attempt / 'result.json').exists() and not supervisor_alive(attempt):
                report = result_report(attempt, active)
                reply = self.event(active, 'finish', **report)
                require(reply['status'] == 'finished' and all(reply[key] == value for key, value in report.items()),
                        '协调服务的完成回执与本机不匹配')
                if not (attempt / 'ack.json').exists():
                    write_record(attempt / 'ack.json', report)
                self.active_file.unlink()
                return 'finished'
            if active['phase'] == 'claimed':
                write_record(attempt / 'launch.json', {key: value for key, value in active.items() if key not in ('ticket', 'phase')})
                reply = self.event(active, 'start')
                require(reply['status'] == 'running', '协调服务未确认开始')
                active['phase'] = 'authorized'
                write_record(self.active_file, active)
            if (attempt / 'entered.json').exists():
                if supervisor_alive(attempt):
                    self.event(active, 'renew')
                    return 'running'
                return self.check_conflict(active, attempt)
            self.event(active, 'renew')
            if self.supervisor is None or self.supervisor.poll() is not None:
                self.supervisor = subprocess.Popen(
                    [sys.executable, '-E', '-s', str(WORKER_SCRIPT), '--supervise', str(attempt)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=self.supervisor_env, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            return 'starting'
        except ControlDenied as error:
            if error.code != 409:
                raise
            return self.check_conflict(active, attempt)

    def wait(self, outcome):
        if outcome in ('running', 'starting') and self.supervisor is not None:
            try:
                self.supervisor.wait(timeout=POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        elif outcome not in ('finished', 'released'):
            time.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--once', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--supervise', type=Path, help=argparse.SUPPRESS)
    group.add_argument('--execute', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute is not None:
        return run_child(args.execute)
    if args.supervise is not None:
        try:
            supervise(args.supervise)
            return 0
        except (OSError, ValueError) as error:
            write_record(args.supervise / 'supervisor-error.json', {'error': str(error)[:4096]})
            return 1
    try:
        require(os.name == 'nt' and args.state_dir is not None, '需要 Windows 和明确指定的计算状态目录')
        state = absolute(args.state_dir)
        config = record(args.config)
        client = device_client(config)
        require(client and client.token, '计算端需要设备协调配置和令牌')
        projects = worker_projects(client, config['computeWorker'], state)
        state.mkdir(parents=True, exist_ok=True)
        lock = ServiceLock(state / 'worker.lockfile')
        try:
            worker = Worker(client, projects, state)
            previous = None
            while True:
                try:
                    outcome = worker.step()
                except ControlDenied:
                    if args.once:
                        raise
                    outcome = 'coordinator_unavailable'
                if args.once:
                    print(json.dumps({'status': outcome}))
                    return 0
                if outcome != previous:
                    print(json.dumps({'status': outcome}), file=sys.stderr, flush=True)
                    previous = outcome
                worker.wait(outcome)
        finally:
            lock.close()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f'计算执行端停止：{getattr(error, "detail", None) or error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
