import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from portable_update import powershell, register_start_menu_shortcut


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows Start menu shortcut")
class StartMenuTests(unittest.TestCase):
    def test_same_shortcut_tracks_the_newer_running_version(self):
        with tempfile.TemporaryDirectory(prefix="澜汐 shortcut ") as directory:
            root = Path(directory)
            versions = root / "versions"
            programs = root / "Start menu" / "Programs"
            old = self.executable(versions, 40)
            new = self.executable(versions, 41)
            register_start_menu_shortcut(old, versions, programs)
            self.assert_link(programs, old)
            register_start_menu_shortcut(new, versions, programs)
            self.assert_link(programs, new)
            register_start_menu_shortcut(old, versions, programs)
            self.assert_link(programs, new)
            self.assertEqual([path.name for path in programs.iterdir()], ["澜汐.lnk"])

    @staticmethod
    def executable(versions, release):
        result = versions / f"Lanxi-R{release}-Portable" / f"Lanxi-Glass-R{release}-Portable.exe"
        result.parent.mkdir(parents=True)
        result.touch()
        return result

    def assert_link(self, programs, expected):
        path = programs / "澜汐.lnk"
        command = powershell("""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$shell = New-Object -ComObject WScript.Shell
$entry = $shell.CreateShortcut($env:LANXI_TEST_FILE)
$entry.TargetPath
$entry.WorkingDirectory
""")
        output = subprocess.check_output(command, env={**os.environ, "LANXI_TEST_FILE": str(path)},
                                         stderr=subprocess.PIPE, timeout=12)
        target, working_directory = output.decode("utf-8").strip().splitlines()
        self.assertEqual(Path(target), expected)
        self.assertEqual(Path(working_directory), expected.parent)
