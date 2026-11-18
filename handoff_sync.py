"""Publish and stage immutable handoff bundles over a paired filesystem folder."""
import argparse
import hmac
import json
import os
import re
import tempfile
import uuid
import zipfile
from pathlib import Path

from handoff import (DIGEST, GIT_ID, HandoffError, capture, file_hash, git, limited, local_path,
                     find_session, names, require, restore, resume_context, session_info, session_progress, sha,
                     switch_pending)
from service_lock import ServiceLock


IDENTIFIER = re.compile('[a-z0-9][a-z0-9_-]{0,39}')
OFFER_LIMIT = 16384
FAILURES = (ValueError, TypeError, KeyError, AttributeError, OSError, IndexError, zipfile.BadZipFile)


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), '项目和设备需使用小写 ASCII 标识')
    return value


def absolute(value):
    location = Path(value)
    require(location.is_absolute(), '交换、项目与预备目录必须使用绝对路径')
    return location.resolve()


def separate(left, right):
    require(not left.is_relative_to(right) and not right.is_relative_to(left), '交换目录与本机工作区需要独立')


def encode(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def read_offer(path, key, project, device):
    record = json.loads(limited(path, OFFER_LIMIT))
    require(isinstance(record, dict), '发布清单格式无效')
    signature = record.pop('signature')
    require(isinstance(signature, str) and DIGEST.fullmatch(signature)
            and hmac.compare_digest(signature, hmac.digest(key, encode(record), 'sha256').hex()),
            '发布清单签名不匹配')
    require(record['version'] == 1 and record['project'] == project and record['device'] == device,
            '发布清单项目或设备不一致')
    require(type(record['revision']) is int and 1 <= record['revision'] < 2**53
            and DIGEST.fullmatch(record['sha256']) and DIGEST.fullmatch(record['snapshot'])
            and GIT_ID.fullmatch(record['head']) and isinstance(record['thread'], str)
            and str(uuid.UUID(record['thread'])) == record['thread']
            and isinstance(record['turn'], str) and 0 < len(record['turn']) <= 128, '发布清单内容无效')
    if 'lfs' in record:
        from lfs_assets import inventory

        inventory(record)
    return record


def atomic_write(path, data, scratch=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=scratch or path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def offer_status(offer):
    return {key: offer[key] for key in ('revision', 'sha256', 'snapshot', 'head', 'thread', 'turn', 'lfs') if key in offer}


def package_expectations(offer):
    return {key: offer[key] for key in ('head', 'thread', 'turn', 'snapshot', 'lfs') if key in offer}


def chain_conflict(old, offer):
    return old and (offer['revision'] < old['revision']
                    or (offer['revision'] == old['revision'] and offer['sha256'] != old['sha256']))


def publishing_repo(repo, home, project, thread, receiver):
    if not receiver:
        return repo
    candidates = find_session(home, thread)
    if len(candidates) != 1:
        return repo  # capture reports the missing or ambiguous native history.
    _, _, workspace = session_info(limited(candidates[0]))
    workspace = Path(workspace).resolve()
    staging = local_path(absolute(receiver['staging']), project)
    if workspace.is_relative_to(repo) or not workspace.is_relative_to(staging) or workspace == staging:
        return repo
    candidate = local_path(staging, workspace.relative_to(staging).parts[0])
    receipt = resume_context(candidate)
    require(receipt is not None and receipt.project == project and receipt.thread == thread,
            '最近的工作目录缺少对应项目的恢复回执')
    # Require the receipt at this staged repository's root. A nested worktree
    # or an unrelated checkout cannot become an automatic publication source.
    require(receipt.private == candidate / '.git' / 'lanxi', '恢复回执目录不一致')
    return candidate


def publish(exchange, project, device, key, settings, home, prior, receiver=None, lfs_memo=None):
    thread = settings['thread']
    registered = absolute(settings['repo'])
    require(not switch_pending(registered), '原工作区有尚未结束的切换')
    repo = publishing_repo(registered, home, project, thread, receiver)
    separate(exchange, repo)
    root = local_path(exchange, project)
    root.mkdir(exist_ok=True)
    objects = local_path(root, 'objects')
    objects.mkdir(exist_ok=True)
    manifest_file = local_path(root, device + '.json')
    current = read_offer(manifest_file, key, project, device) if manifest_file.exists() else None
    if prior and (not current or chain_conflict(prior, current)):
        return {'status': 'version_conflict', 'lastPublished': prior}
    previous = None
    if current:
        package_path = local_path(objects, current['sha256'] + '.zip')
        if package_path.is_file():
            previous = current['snapshot']
    base = settings['base']
    if repo != registered:
        base = git(registered, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
    allowed = settings.get('include', [])
    require(isinstance(allowed, list), 'include 需要文件列表')
    for filename in allowed:
        local_path(repo, filename)
    untracked = set(names(git(repo, 'ls-files', '--others', '--exclude-standard', '-z'))) if allowed else set()
    includes = sorted(untracked.intersection(allowed))
    lfs_paths = settings.get('lfs', [])
    require(isinstance(lfs_paths, list), 'publish.lfs 需要文件列表')
    # The sibling temp directory is outside the folder watched by the transport,
    # and on the same volume for atomic publication of the final names.
    with tempfile.TemporaryDirectory(dir=exchange.parent) as scratch:
        temporary = Path(scratch) / 'sealed.zip'
        result = capture(repo, home, project, base, thread, includes, temporary, previous_snapshot=previous, publish=True,
                         lfs_paths=lfs_paths, lfs_memo=lfs_memo)
        if result is not None:
            revision = current['revision'] + 1 if current else 1
            current = dict(version=1, device=device, revision=revision, **result)
            signed = dict(current, signature=hmac.digest(key, encode(current), 'sha256').hex())
            encoded = encode(signed)
            require(len(encoded) <= OFFER_LIMIT, '签名交接清单超过容量上限')
            package = local_path(objects, result['sha256'] + '.zip')
            try:
                os.link(temporary, package)
            except FileExistsError:
                with package.open('rb') as stream:
                    require(file_hash(stream) == result['sha256'], '同名交接包已损坏')
            atomic_write(manifest_file, encoded, scratch)
    return {'status': 'published', 'lastPublished': offer_status(current)}


def inspect_stage(dest, project, offer, home, lfs_settings=None, environment=None, lfs_memo=None, seed=None):
    private = dest / '.git' / 'lanxi'
    marker = json.loads(limited(private / 'handoff.json', 65536))
    require(marker['version'] == 1 and marker['project'] == project and marker['package_sha256'] == offer['sha256']
            and all(marker[field] == offer[field] for field in ('head', 'thread', 'turn'))
            and marker.get('lfs', {}) == offer.get('lfs', {}),
            '预备目录已存在且回执不同')
    transcript = limited(private / 'session.jsonl')
    require(sha(transcript) == marker['sha256'], '预备目录会话与回执不符')
    cwd = dest if marker['cwd'] == '.' else local_path(dest, marker['cwd'])
    require(cwd.is_dir(), '预备目录缺少会话工作目录')
    try:
        progress, _, _ = session_progress(home, offer['thread'], transcript)
    except HandoffError:
        status = 'session_conflict'
    else:
        status = 'advance_on_resume' if progress == 'advance' else 'ready'
    result = {'status': status, 'cwd': str(cwd), 'lastStaged': {**offer_status(offer), 'cwd': str(cwd)}}
    if offer.get('lfs') and status != 'session_conflict':
        from lfs_assets import HTTPSource, copy_cache, inventory, prefetch_inventory, verify_worktree

        selected = inventory(offer)
        source = None
        if lfs_settings is not None:
            try:
                paths = lfs_settings['paths']
                require(isinstance(paths, list) and set(selected) <= set(paths), '来源 LFS 路径未经本机批准')
                user_var, token_var = lfs_settings.get('userEnv'), lfs_settings.get('tokenEnv')
                username, token = environment.get(user_var), environment.get(token_var)
                require(not (user_var or token_var) or (username and token), '缺少 LFS 下载凭据')
                if lfs_settings.get('endpoint'):
                    source = HTTPSource(lfs_settings['endpoint'], username, token)
            except FAILURES:
                return dict(result, status='lfs_unconfigured')
        try:
            verify_worktree(dest, selected, lfs_memo)
        except FAILURES:
            return dict(result, status='lfs_staging_changed')
        files = prefetch_inventory(dest, selected, memo=lfs_memo)['files']
        missing = {name: selected[name] for name, row in files.items() if row['status'] == 'missing_cache'}
        if missing:
            cached = prefetch_inventory(seed, missing, source, lfs_memo)['files']
            reusable = {name: missing[name] for name, row in cached.items() if row['status'] == 'ready'}
            if reusable:
                copy_cache(seed, dest, reusable, lfs_memo)
            files.update(cached)
        result['lfs'] = {name: {key: row[key] for key in ('status', 'oid', 'size')} for name, row in files.items()}
        if any(row['status'] != 'ready' for row in files.values()):
            result['status'] = 'lfs_pending'
    return result


def stage_path(staging, project, peer, offer):
    folder = local_path(staging, project)
    return local_path(folder, f"{peer}-{offer['revision']}-{offer['sha256'][:16]}")


def receive(exchange, project, peer, key, settings, home, prior, environment=None, lfs_memo=None):
    note = {'device': peer, 'lastStaged': prior}
    root = local_path(exchange, project)
    try:
        offer = read_offer(local_path(root, peer + '.json'), key, project, peer)
    except FileNotFoundError:
        return dict(note, status='awaiting_manifest')
    except FAILURES:
        return dict(note, status='invalid_manifest')
    if chain_conflict(prior, offer):
        return dict(note, status='version_conflict')
    seed, staging = absolute(settings['seed']), absolute(settings['staging'])
    separate(exchange, seed)
    separate(exchange, staging)
    separate(seed, staging)
    dest = stage_path(staging, project, peer, offer)
    dest.parent.mkdir(parents=True, exist_ok=True)
    details = dict(note, offer=offer_status(offer))
    if not dest.exists():
        package = local_path(root, 'objects/' + offer['sha256'] + '.zip')
        if not package.is_file():
            return dict(details, status='awaiting_package')
        try:
            restore(package, offer['sha256'], project, seed, dest,
                    expected=package_expectations(offer))
        except FAILURES:
            return dict(details, status='restore_failed')
    try:
        return {**details, **inspect_stage(dest, project, offer, home, settings.get('lfs'), environment, lfs_memo, seed)}
    except FAILURES:
        return dict(details, status='staging_conflict')


def state_path(runtime):
    return runtime / '.local' / 'handoff-sync.json'


def read_state(runtime):
    try:
        state = json.loads(limited(state_path(runtime), 2 * 1024 * 1024))
    except FileNotFoundError:
        return {'projects': {}}
    require(isinstance(state, dict) and isinstance(state.get('projects'), dict), '交接状态文件格式无效')
    return state


def sync_once(runtime, config, home, environment=None, *, control=None, stop=None, lfs_memo=None):
    runtime = runtime.resolve()
    device = identifier(config['device'])
    environment = os.environ if environment is None else environment
    previous = read_state(runtime)
    result = {}
    for project, settings in config.get('projects', {}).items():
        result[project] = {'received': []}
        row = result[project]
        old = previous['projects'].get(project, {})
        try:
            identifier(project)
            key = environment.get(settings['keyEnv'], '').encode()
            require(len(key) >= 32, '每项目的交接密钥须至少 32 字节')
            exchange = absolute(settings['exchange'])
            require(exchange.is_dir() and exchange != exchange.parent, '需要独立的同步子目录')
            separate(exchange, runtime)
        except FAILURES:
            result[project] = dict(old, status='unavailable')
            continue
        if 'receive' in settings:
            conf = settings['receive']
            if not isinstance(conf, dict) or not isinstance(conf.get('peers'), list):
                row['status'] = 'unavailable'
                row['received'] = old.get('received', [])
            else:
                priors = {r['device']: r.get('lastStaged') for r in old.get('received', [])}
                for peer in sorted({p for p in conf['peers'] if isinstance(p, str)}):
                    prior = priors.get(peer)
                    try:
                        identifier(peer)
                        require(peer != device, '接收设备不能填写自己')
                        row['received'].append(receive(exchange, project, peer, key, conf, home, prior, environment, lfs_memo))
                    except FAILURES:
                        row['received'].append({'device': peer, 'status': 'unavailable', 'lastStaged': prior})
                if conf.get('autoApply', False) is not False:
                    from handoff_auto import update

                    row['autoApply'] = update(project, device, exchange, key, conf, row['received'], home, control,
                                              old.get('autoApply', {}), stop, lfs_memo)
        if 'publish' in settings:
            prior = old.get('publish', {}).get('lastPublished')
            try:
                row['publish'] = publish(exchange, project, device, key, settings['publish'], home, prior,
                                         settings.get('receive'), lfs_memo)
            except FAILURES:
                row['publish'] = {'status': 'capture_blocked', 'lastPublished': prior}
    state = {'projects': result}
    if state != previous:
        path = state_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, encode(state))
    return state


def local_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()


def auto_control(configuration):
    if configuration is not None:
        from coordination_client import device_client

        try:
            return device_client({'coordination': configuration})
        except FAILURES:
            return None


def run(runtime, configuration, stop, notify, coordination=None):
    try:
        delay = max(5, int(configuration.get('scanSeconds', 15)))
        lock = ServiceLock(runtime / 'handoff-sync.lockfile')
    except (RuntimeError, ValueError, TypeError, OSError, AttributeError):
        notify('unavailable')
        return
    try:
        control = auto_control(coordination)
        lfs_memo = {}
        while not stop.is_set():
            try:
                sync_once(runtime, configuration, local_home(), control=control, stop=stop, lfs_memo=lfs_memo)
                notify('running')
            except FAILURES:
                notify('unavailable')
            stop.wait(delay)
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('local-config.json'))
    parser.add_argument('--codex-home', type=Path, default=local_home())
    args = parser.parse_args()
    runtime = args.config.resolve().parent
    try:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        lock = ServiceLock(runtime / 'handoff-sync.lockfile')
        try:
            result = sync_once(runtime, config['handoffSync'], args.codex_home.resolve(),
                               control=auto_control(config.get('coordination')))
        finally:
            lock.close()
        print(json.dumps(result))
    except (*FAILURES, RuntimeError) as error:
        parser.exit(1, f'交接扫描失败：{error}\n')


if __name__ == '__main__':
    main()
