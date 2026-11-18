"""Build local installables using Python and the installed PyCharm JDK."""
import argparse
import io
import json
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]


def vscode(output):
    source = ROOT / 'ide/vscode'
    info = json.loads((source / 'package.json').read_text(encoding='utf-8'))
    manifest = ET.Element('PackageManifest', Version='2.0.0',
                          xmlns='http://schemas.microsoft.com/developer/vsx-schema/2011')
    metadata = ET.SubElement(manifest, 'Metadata')
    ET.SubElement(metadata, 'Identity', Id=info['name'], Version=info['version'],
                  Publisher=info['publisher'], Language='en-US')
    ET.SubElement(metadata, 'DisplayName').text = info['displayName']
    properties = ET.SubElement(metadata, 'Properties')
    ET.SubElement(properties, 'Property', Id='Microsoft.VisualStudio.Code.Engine', Value=info['engines']['vscode'])
    ET.SubElement(ET.SubElement(manifest, 'Installation'), 'InstallationTarget', Id='Microsoft.VisualStudio.Code')
    ET.SubElement(manifest, 'Dependencies')
    ET.SubElement(ET.SubElement(manifest, 'Assets'), 'Asset', Type='Microsoft.VisualStudio.Code.Manifest',
                  Path='extension/package.json', Addressable='true')
    types = ET.Element('Types', xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    for extension, mime in (('vsixmanifest', 'text/xml'), ('json', 'application/json'), ('js', 'application/javascript')):
        ET.SubElement(types, 'Default', Extension=extension, ContentType=mime)
    with ZipFile(output / 'lanxi-activity.vsix', 'x', ZIP_DEFLATED) as package:
        package.writestr('extension.vsixmanifest', ET.tostring(manifest, encoding='utf-8', xml_declaration=True))
        package.writestr('[Content_Types].xml', ET.tostring(types, encoding='utf-8', xml_declaration=True))
        for filename in ('package.json', 'extension.js'):
            package.write(source / filename, f'extension/{filename}')


def pycharm(output, ide):
    source = ROOT / 'ide/pycharm'
    javac = ide / 'jbr/bin/javac.exe'
    version = (ide / 'build.txt').read_text().strip().removeprefix('PY-')
    if version.split('.')[0] != '263':
        raise ValueError(f'插件清单匹配 PyCharm 构建 263，检测到 {version}')
    with tempfile.TemporaryDirectory(dir=output) as folder:
        classes = Path(folder)
        subprocess.run([str(javac), '-encoding', 'UTF-8', '-classpath', str(ide / 'lib/*'),
                        '-d', folder, str(source / 'src/LanxiProjectListener.java')], check=True)
        jar = io.BytesIO()
        with ZipFile(jar, 'w', ZIP_DEFLATED) as plugin:
            plugin.write(source / 'resources/META-INF/plugin.xml', 'META-INF/plugin.xml')
            for file in sorted(classes.rglob('*.class')):
                plugin.write(file, file.relative_to(classes).as_posix())
    with ZipFile(output / 'lanxi-activity-pycharm.zip', 'x', ZIP_DEFLATED) as package:
        package.writestr('lanxi-activity/lib/lanxi-activity.jar', jar.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pycharm', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    vscode(output)
    pycharm(output, args.pycharm.resolve())
    print(output)


if __name__ == '__main__':
    main()
