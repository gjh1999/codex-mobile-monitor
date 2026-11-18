"""Select an authenticated, explicitly pinned handoff for an idle worktree."""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from coordination_client import ControlDenied
from handoff import (HandoffError, limited, local_path, require, session_progress, sha, switch_pending,
                     verified_bundle)
from handoff_apply import clean, switch
from handoff_merge import SnapshotChanged, WorkspaceDirty, prepare_merge
from handoff_sync import FAILURES, absolute, chain_conflict, identifier, offer_status, read_offer, separate, stage_path


RETRY_SECONDS = 60


class OfferChanged(HandoffError):
    pass


def applied_receipt(root, project, thread):
    marker = root / '.git/lanxi/handoff.json'
    if marker.is_file():
        record = json.loads(limited(marker, 65536))
        if record.get('applied') and (record['project'], record['thread']) == (project, thread):
            return record
    return {}


def update(project, device, exchange, key, receive, received, home, control, previous, stop=None):
    config = receive['autoApply']
    try:
        peer, thread, branch = identifier(config['peer']), config['thread'], config['branch']
        require(str(uuid.UUID(thread)) == thread and isinstance(branch, str) and branch and peer in receive['peers'],
                '自动更新需要登记接收端、原生线程和本机分支')
        require(control is not None and control.token and control.device == device
                and control.threads.get(thread) == project, '自动更新需要同设备的协调配置和登记线程')
        root, = (path for path, name in control.paths.items() if name == project)
        staging = absolute(receive['staging'])
        separate(staging, root)
        separate(exchange, root)
    except FAILURES:
        return {'status': 'invalid_config'}
    if os.name != 'nt':
        return {'status': 'unsupported_platform'}

    row = next((entry for entry in received if entry['device'] == peer), {})
    offer = row.get('offer')
    prior = previous.get('lastApplied') or {}
    if (prior.get('device'), prior.get('thread')) != (peer, thread):
        prior = {}
    result = dict(device=peer, branch=branch, offer=offer, lastApplied=prior or None)
    try:
        if switch_pending(root):
            return dict(result, status='recovery_required')
        if not offer or offer['thread'] != thread:
            return dict(result, status='thread_mismatch' if offer else row.get('status', 'waiting_for_snapshot'))

        selected = dict(offer, device=peer, branch=branch)
        receipt = applied_receipt(root, project, thread)
        durable = receipt.get('automatic', {})
        if (durable.get('device'), durable.get('thread')) == (peer, thread) and durable['revision'] >= prior.get('revision', 0):
            prior = durable
            result['lastApplied'] = prior
        if chain_conflict(prior, offer):
            return dict(result, status='version_conflict')
        if receipt.get('package_sha256') == offer['sha256']:
            return dict(result, status='applied', lastApplied=selected, head=receipt['head'], cwd=str(root / receipt['cwd']))
        if prior and prior['sha256'] == offer['sha256']:
            return dict(result, status='already_seen')
        if row['status'] not in ('ready', 'advance_on_resume'):
            return dict(result, status=row['status'])
        if stop is not None and stop.is_set():
            return dict(result, status='deferred')
        same_offer = (previous.get('device'), previous.get('branch'), previous.get('offer')) == (peer, branch, offer)
        if same_offer and 0 < previous.get('retryAt', 0) - time.time() <= RETRY_SECONDS:
            return previous

        occupancy = control.request(project, 'control')
        if occupancy['writer'] or occupancy['humans']:
            return dict(result, status='occupied')
        before, ref = clean(root)
        if ref != 'refs/heads/' + branch:
            return dict(result, status='branch_mismatch')
        if same_offer and previous.get('localHead') == before and previous.get('status') in ('conflict', 'history_unavailable'):
            return previous

        stage = stage_path(staging, project, peer, offer)
        shared = local_path(exchange, project)
        package = local_path(shared, 'objects/' + offer['sha256'] + '.zip')
        expected = {name: offer[name] for name in ('head', 'thread', 'turn', 'snapshot')}
        with verified_bundle(package, offer['sha256'], project, expected) as (_, manifest):
            transcript = limited(stage / '.git/lanxi/session.jsonl')
            if sha(transcript) != manifest['objects']['session.jsonl']:
                raise SnapshotChanged('预备会话与签名包不符')
            if session_progress(home, thread, transcript, distinguish_ahead=True)[0] == 'ahead':
                return dict(result, status='local_session_ahead')

        def verify_offer():
            current = read_offer(local_path(shared, peer + '.json'), key, project, peer)
            if offer_status(current) != offer or (stop is not None and stop.is_set()):
                raise OfferChanged('签名清单或本机运行状态已变化')

        with tempfile.TemporaryDirectory(dir=stage.parent, prefix='.lanxi-auto-') as temporary:
            prepared = Path(temporary) / 'merge'
            merge = prepare_merge(root, stage, prepared, verified=manifest)
            if merge['status'] != 'merged':
                details = {field: merge[field] for field in ('layer', 'paths', 'history') if field in merge}
                return dict(result, status=merge['status'], localHead=before, **details)
            verify_offer()
            switched = switch(control, project, prepared, home, automatic=selected, revalidate=verify_offer)
        return dict(result, status='applied', lastApplied=selected, head=switched['head'], cwd=switched['cwd'])
    except WorkspaceDirty:
        return dict(result, status='local_changes')
    except SnapshotChanged:
        return dict(result, status='staging_changed', retryAt=time.time() + RETRY_SECONDS)
    except OfferChanged:
        return dict(result, status='deferred')
    except ControlDenied as error:
        status = 'recovery_required' if switch_pending(root) else 'occupied' if error.code == 409 else 'coordination_unavailable'
        return dict(result, status=status)
    except (*FAILURES, RuntimeError):
        if switch_pending(root):
            return dict(result, status='recovery_required')
        return dict(result, status='blocked', retryAt=time.time() + RETRY_SECONDS)
