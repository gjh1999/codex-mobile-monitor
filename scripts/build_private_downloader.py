"""Generate the standalone downloader distributed with a private workspace grant."""

import argparse
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud_pairing import MAX_CODE, WORKSPACE_PREFIX, decode_invite


def write_downloader(grant, destination):
    grant = grant.strip()
    decode_invite(grant, prefix=WORKSPACE_PREFIX)
    template = Path(__file__).with_name('download-latest-portable.cmd').read_text(encoding='utf-8')
    marker = '__LANXI_WORKSPACE_GRANT__'
    if template.count(marker) != 1:
        raise ValueError('下载模板缺少唯一的授权占位符')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(template.replace(marker, grant), encoding='utf-8', newline='\r\n')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def release_downloader(grant):
    destination = ROOT / 'release' / '笔记本下载' / '下载澜汐最新版.cmd'
    write_downloader(grant, destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace_grant', type=Path)
    args = parser.parse_args()
    with args.workspace_grant.open(encoding='ascii') as source:
        print(release_downloader(source.read(MAX_CODE + 1)))
