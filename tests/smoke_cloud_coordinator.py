"""Start the deployed CLI over TLS and check both sides of personal sync."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from cloud_pairing import create_invite, join
from coordination_client import ControlClient, device_token
from personal_relay import EMPTY, MAX_RECORD, Changed, RemoteRecords

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "deploy/coordinator/cloud_admin.py"


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def serve(root, port):
    command = [
        sys.executable,
        "-B",
        str(REPO / "coordination_server.py"),
        "--devices",
        str(root / "devices.json"),
        "--database",
        str(root / "coordination.sqlite3"),
        "--port",
        str(port),
        "--no-access-log",
        "--limit-concurrency",
        "24",
        "--tls-cert",
        str(root / "cert.pem"),
        "--tls-key",
        str(root / "key.pem"),
    ]
    with tempfile.TemporaryFile() as log, subprocess.Popen(command, stdout=log, stderr=log) as proc:
        url = f"https://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 12
            while proc.poll() is None and time.monotonic() < deadline:
                try:
                    with urlopen(url + "/healthz", timeout=0.5) as response:
                        assert json.load(response) == {"status": "ready"}
                    break
                except (URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                log.seek(0)
                raise RuntimeError(log.read().decode("utf-8", errors="replace"))
            yield url
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


def admin(root, *args):
    return subprocess.run(
        [sys.executable, "-B", str(ADMIN), "--file", str(root / "devices.json"), *args],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=10,
        check=False,
    )


def http_error(action, expected):
    try:
        action()
    except HTTPError as error:
        assert error.code == expected, error.code
        error.close()
    else:
        raise AssertionError(f"Expected HTTP {expected}")


def clients_roundtrip(root, url, devices):
    # The production client uses native Windows writer locks for installed rollouts.
    from code_sync import value as git
    from personal_sync import PersonalSync
    from tests.smoke_personal_exe import (
        MEMORY,
        PROJECT,
        REMOTE,
        hash_file,
        isolated_git,
        session_path,
        source_fixture,
    )

    origin = root / "git-fixture"
    origin.mkdir()
    source = root / "first" / "source-work"
    stop = threading.Event()
    with isolated_git(root, origin):
        machines = [
            PersonalSync(
                root / name,
                {"checkoutRoot": str(root / name / "projects"), "projects": projects},
                {"url": url, "device": name, "projects": {}},
                home=root / name / "codex",
            )
            for name, projects in (
                ("first", [{"remote": REMOTE, "path": str(source), "memoryPath": MEMORY.as_posix()}]),
                ("second", []),
            )
        ]
        a, b = machines
        fixture = source_fixture(origin, source, a.home)

        def run(machine):
            with patch.dict(os.environ, LANXI_DEVICE_TOKEN=devices[machine.coordination["device"]]["token"]):
                machine.cycle(stop)
            assert machine.snapshot()["status"] == {PROJECT: "已同步"}, machine.snapshot()["status"]

        run(a)
        run(b)
        checkout = Path(b.snapshot()["projects"][PROJECT])
        assert (checkout / MEMORY / "decision.md").read_bytes() == b"source note\n"
        assert (b.home / "AGENTS.md").is_file()
        assert hash_file(b.home / session_path(fixture)) == fixture["session_hash"]
        assert fixture["id"] in b.snapshot()["sessions"]
        assert not (checkout / "fixture-large.bin").exists()
        (checkout / "app.py").write_bytes(b"# hello from receiver\n")
        (checkout / MEMORY / "decision.md").write_bytes(b"receiver note\n")
        run(b)
        run(a)
        assert (source / "app.py").read_bytes() == b"# hello from receiver\n"
        assert (source / MEMORY / "decision.md").read_bytes() == b"receiver note\n"
        assert git(source, "rev-parse", "HEAD") == git(checkout, "rev-parse", "HEAD")


def verify(root):
    started = time.monotonic()
    devices_file = root / "devices.json"
    assert admin(root, "init", "first", "second", "other").returncode == 0
    original = devices_file.read_bytes()
    assert admin(root, "init", "replacement").returncode == 1 and devices_file.read_bytes() == original
    devices = json.loads(original)
    assert len({device["token"] for device in devices.values()}) == 3
    local_url = "http://127.0.0.1:18770"
    local_pair = admin(root, "pair", "first", "--url", local_url)
    assert local_pair.returncode == 0
    assert json.loads(local_pair.stdout.split("\n\n", 1)[0])["coordination"]["url"] == local_url
    assert admin(root, "pair", "first", "--url", "http://sync.example.com").returncode == 1
    devices["other"]["personalSpace"] = "another-test-space"
    devices_file.write_text(json.dumps(devices), encoding="utf-8")
    key, cert = root / "key.pem", root / "cert.pem"
    openssl = shutil.which("openssl")
    assert openssl, "OpenSSL is required for the loopback TLS fixture"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )

    payload = b"note and completed session fixture"
    large = b"x" * (MAX_RECORD - 1)
    key_id = "fixture/persistent"
    port = available_port()
    paired = None

    def client(url, name, token=None):
        return RemoteRecords(
            ControlClient({"url": url, "device": name, "projects": {}}, token or devices[name]["token"])
        )

    with patch.dict(os.environ, SSL_CERT_FILE=str(cert), NO_PROXY="localhost,127.0.0.1"):
        with serve(root, port) as url:
            config = admin(root, "pair", "second", "--url", url)
            assert config.returncode == 0
            assert json.loads(config.stdout.split("\n\n", 1)[0])["coordination"]["device"] == "second"
            assert (
                devices["second"]["token"] in config.stdout and devices["first"]["token"] not in config.stdout
            )
            a, b, other = (client(url, name) for name in devices)
            http_error(client(url, "first", "wrong").index, 403)
            a.put(key_id, EMPTY, payload)
            assert b.read(key_id) == payload
            assert key_id not in other.index()
            http_error(lambda: other.read(key_id), 404)
            try:
                b.put(key_id, EMPTY, b"stale")
            except Changed:
                pass
            else:
                raise AssertionError("A stale write replaced an existing record")
            a.put("fixture/large", EMPTY, large)
            assert hashlib.sha256(b.read("fixture/large")).digest() == hashlib.sha256(large).digest()
            http_error(lambda: a.put("fixture/too-large", EMPTY, large + b"x"), 413)
            if os.name == "nt":
                clients_roundtrip(root, url, devices)
                desktop = {"url": url, "device": "first", "projects": {},
                           "trustedCertificate": cert.read_text(encoding="ascii")}
                laptop, folder = root / "laptop-config", root / "laptop-projects"
                laptop.mkdir()
                folder.mkdir()
                (laptop / "local-config.json").write_text('{"desktopWallpaperMinutes":15}', encoding="utf-8")
                with patch.dict(os.environ, LANXI_DEVICE_TOKEN=devices["first"]["token"]):
                    invitation = create_invite(desktop)
                # The new client uses only the certificate in its invitation.
                with patch.dict(os.environ, SSL_CERT_FILE=str(root / "missing.pem"), LANXI_DEVICE_TOKEN=""):
                    join(laptop, invitation, folder)
                    join(laptop, invitation, folder)
                    saved = json.loads((laptop / "local-config.json").read_bytes())
                    assert saved["desktopWallpaperMinutes"] == 15
                    assert Path(json.loads((laptop / "project-library.json").read_bytes())["root"]) == folder
                    paired = saved["coordination"]
                    assert paired["device"] != "first"
                    assert device_token(paired) not in json.dumps(saved)
                    paired_records = RemoteRecords(ControlClient(paired, device_token(paired)))
                    assert paired_records.read(key_id) == payload
                    duplicate = root / "different-device"
                    duplicate.mkdir()
                    try:
                        join(duplicate, invitation, folder)
                    except ValueError as error:
                        assert "已过期或已使用" in str(error)
                    else:
                        raise AssertionError("A second device reused a spent invitation")
        with serve(root, port) as url:
            assert client(url, "second").read(key_id) == payload
            if paired:
                assert RemoteRecords(ControlClient(paired, device_token(paired))).read(key_id) == payload
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "https": True,
        "device_isolation": True,
        "conflict_preserved": True,
        "persisted_after_restart": True,
        "accepted_bytes": len(large),
        "oversize_http": 413,
        "windows_clients_roundtrip": os.name == "nt",
        "windows_pairing_restart_persistence": paired is not None,
        "seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lanxi-https-") as scratch:
        report = verify(Path(scratch))
    options.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
