"""Transfer registered compute outputs without writing into a project worktree."""
import json
import os

from compute_artifacts import CHUNK, MANIFEST_LIMIT, append_block, manifest
from handoff import local_path, require, sha
from handoff_merge import separate
from handoff_sync import absolute
from lfs_assets import file_size, matches, publish
from service_lock import ServiceLock


TIMEOUT = 30


def endpoint(job):
    return f'compute/jobs/{job}/artifacts'


def request(client, project, target, limit, **kwargs):
    with client.open(project, target, timeout=TIMEOUT, **kwargs) as response:
        require(response.headers.get('Content-Encoding', 'identity').lower() == 'identity', '不支持产物响应压缩')
        data = response.read(limit + 1)
    require(len(data) <= limit, '产物响应超出大小限制')
    return data


def upload_outputs(client, active, instance, attempt, raw, outputs):
    project = active['project']
    target = endpoint(active['id'])
    headers = {'X-Lanxi-Instance': instance, 'X-Lanxi-Ticket': active['ticket'],
               'Content-Type': 'application/octet-stream'}
    progress = json.loads(request(client, project, target, MANIFEST_LIMIT, method='POST', data=raw, headers=headers))
    offsets = progress['offsets']
    require(progress['sha256'] == sha(raw) and isinstance(offsets, list) and len(offsets) == len(outputs),
            '协调中心的产物进度与本机清单不符')
    for index, (item, offset) in enumerate(zip(outputs, offsets)):
        require(type(offset) is int and 0 <= offset <= item['bytes'], '上传进度偏移无效')
        if offset == item['bytes']:
            continue
        path = local_path(attempt / 'work', item['path'])
        with path.open('rb') as stream:
            while offset < item['bytes']:
                stream.seek(offset)
                length = min(CHUNK, item['bytes'] - offset)
                data = stream.read(length)
                require(len(data) == length, '上传期间产物大小变化')
                status = json.loads(request(client, project, f'{target}/{index}?offset={offset}', MANIFEST_LIMIT,
                                            method='PUT', data=data, headers=headers))
                offset_after = status['offset']
                require(type(offset_after) is int and offset + length <= offset_after <= item['bytes'],
                        '协调中心返回的分块进度无效')
                offset = offset_after


def download_outputs(client, project, job_id, output):
    root = absolute(output)
    require(all(separate(root, repo) for repo in client.paths), '产物下载目录需要独立于登记项目')
    status = client.request(project, f'compute/jobs/{job_id}')
    require(status['id'] == job_id and status['status'] == 'finished' and status['exitCode'] == 0,
            '当前没有成功完成的作业产物')
    target = endpoint(job_id)
    raw = request(client, project, target, MANIFEST_LIMIT)
    require(sha(raw) == status['resultSha256'], '产物清单和完成回执不匹配')
    outputs = manifest(raw, root, project, status)
    scratch = local_path(root.parent, '.lanxi-download-' + sha(os.fsencode(root) + raw)[:32])
    scratch.mkdir(parents=True, exist_ok=True)
    lock = ServiceLock(local_path(scratch, 'lockfile'))
    try:
        for index, item in enumerate(outputs):
            path = local_path(root, item['path'])
            if file_size(path) is not None:
                require(matches(path, item['sha256'], item['bytes']), '本机已有不同的产物文件')
                continue
            part = local_path(scratch, f'{index}.part')
            offset = file_size(part)
            if offset is None:
                part.touch(exist_ok=False)
                offset = 0
            while offset < item['bytes']:
                length = min(CHUNK, item['bytes'] - offset)
                block = request(client, project, f'{target}/{index}?offset={offset}', length)
                require(len(block) == length, '产物下载分块不完整')
                append_block(part, block)
                offset += length
            if not matches(part, item['sha256'], item['bytes']):
                part.unlink()
                raise ValueError('下载产物的长度或 SHA-256 校验失败，可重新下载')
            path.parent.mkdir(parents=True, exist_ok=True)
            publish(part, local_path(root, item['path']), item['sha256'], item['bytes'])
    finally:
        lock.close()
    return {'status': 'downloaded', 'id': job_id, 'output': str(root), 'files': len(outputs)}
