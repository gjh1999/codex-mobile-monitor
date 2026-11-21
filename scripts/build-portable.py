"""Build a versioned Windows package for the paired device update service."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from portable_update import LIMIT, VERSION
from cloud_pairing import MAX_CODE, WORKSPACE_PREFIX, WORKSPACE_RESOURCE, decode_invite
from build_private_downloader import release_downloader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--pycharm', type=Path, required=True)
    parser.add_argument('--workspace-grant', type=Path)
    args = parser.parse_args()

    work = args.work.resolve()
    app = f'Lanxi-Glass-R{VERSION}-Portable'
    name = f'Lanxi-R{VERSION}-Windows-Private.zip'
    release = ROOT / 'release' / name
    if release.exists():
        parser.error(f'Package already exists: {release}')
    work.mkdir(parents=True)
    package = work / 'package'
    adapters = work / 'adapters'
    log = work / 'build.log'
    desktop = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(ROOT / 'scripts/build-exe.ps1'), '-AppName', app,
               '-OutputDirectory', str(package), '-BuildDirectory', str(work / 'desktop')]
    if args.workspace_grant:
        with args.workspace_grant.open(encoding='ascii') as stream:
            grant = stream.read(MAX_CODE + 1)
        decode_invite(grant, prefix=WORKSPACE_PREFIX)
        resource = work / WORKSPACE_RESOURCE
        resource.write_text(grant, encoding='ascii')
        desktop += ['-WorkspaceGrant', str(resource)]
    commands = (
        desktop,
        [sys.executable, str(ROOT / 'scripts/build-ide-adapters.py'),
         '--output', str(adapters), '--pycharm', str(args.pycharm.resolve())],
        [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--console',
         '--name', 'Lanxi-IdeActivity', '--paths', str(ROOT), '--distpath', str(adapters),
         '--workpath', str(work / 'ide'), '--specpath', str(work / 'ide-specs'), str(ROOT / 'ide_activity.py')],
    )
    with log.open('wb') as stream:
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            if result.returncode:
                parser.exit(result.returncode, f'Build failed. See {log}\n')

    instructions = package / '开始使用.txt'
    instructions.write_text(
        f'澜汐 R{VERSION}\n\n'
        f'双击 {app}.exe 打开澜汐。旧版正在运行时，先从托盘退出。\n'
        + ('启动时选择「连接云服务器」或「本机使用」。首次使用再选本机项目母目录。\n'
           '同一私人包的各台电脑点「连接云服务器」后会加入同一个工作区。\n'
           if args.workspace_grant else '首次使用时选择本机项目母目录。有邀请码时可同时填写。\n')
        + '已有连接凭据和数据会自动读回。\n'
        '从顶部菜单栏打开「连接云服务器」「测试」或「合流」。合流会立即检查 GitHub 和服务器备份。\n'
        '从「跨机接续」可以下载服务器代码备份。\n'
        '需要在 VS Code 或 PyCharm 中上报项目占用时，解压「IDE上报工具.zip」，'
        '运行「启用IDE上报.cmd」，安装对应插件后重启 IDE。\n',
        encoding='utf-8-sig', newline='\r\n',
    )
    ide_zip = package / 'IDE上报工具.zip'
    with ZipFile(ide_zip, 'x', ZIP_DEFLATED) as archive:
        for filename in ('Lanxi-IdeActivity.exe', 'lanxi-activity.vsix', 'lanxi-activity-pycharm.zip'):
            archive.write(adapters / filename, filename)
        archive.write(ROOT / 'scripts/enable-ide-activity.cmd', '启用IDE上报.cmd')

    archive_path = work / name
    with ZipFile(archive_path, 'x', ZIP_DEFLATED) as archive:
        for file in (package / f'{app}.exe', instructions, ide_zip):
            archive.write(file, file.name)
    size = archive_path.stat().st_size
    if size > LIMIT:
        parser.exit(1, f'Package exceeds the update size limit. See {archive_path}\n')
    with archive_path.open('rb') as source:
        sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
    (work / 'windows-release.json').write_text(
        json.dumps(dict(version=VERSION, sha256=sha256, size=size)) + '\n', encoding='utf-8',
    )
    release.parent.mkdir(exist_ok=True)
    if args.workspace_grant:
        print(release_downloader(grant))
    archive_path.rename(release)
    print(release)


if __name__ == '__main__':
    main()
