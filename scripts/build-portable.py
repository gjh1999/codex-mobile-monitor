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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--pycharm', type=Path, required=True)
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
    commands = (
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
         str(ROOT / 'scripts/build-exe.ps1'), '-AppName', app,
         '-OutputDirectory', str(package), '-BuildDirectory', str(work / 'desktop')],
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
        '首次使用时填写配对码，并选择项目母目录。已有配对和数据会自动读回。\n'
        '在 Codex 或 Claude Code 主窗口左下角点「测试」，也可右键澜汐托盘图标。\n'
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
    archive_path.rename(release)
    print(release)


if __name__ == '__main__':
    main()
