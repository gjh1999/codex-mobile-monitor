"""Start the deployed CLI over TLS and check both sides of personal sync."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zipfile import ZipFile

from cloud_pairing import WORKSPACE_PREFIX, SETUP_PREFIX, create_invite, decode_invite, join, join_workspace, post
from automation import Approvals
from code_sync import run
from codex_watch import SharedCodex
from codex_workspace import CodexWorkspace
from coordination_client import ControlClient, PersonalTaskLocks, device_token
from personal_relay import EMPTY, MAX_RECORD, Changed, RemoteRecords
from portable_update import endpoint, latest, prepare

REPO = Path(__file__).resolve().parents[1]


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
        "--release-dir",
        str(root / "releases"),
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
        [sys.executable, "-B", "-m", "deploy.coordinator.cloud_admin", "--file", str(root / "devices.json"), *args],
        cwd=REPO,
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


def codex_activity_roundtrip(root, source, viewer, other):
    thread = '11111111-2222-4333-8444-555555555555'
    sibling = '22222222-3333-4444-8555-666666666666'
    project = 'lanxi-approval-fixture/control-review'
    cwd = root / 'approval-repo'
    cwd.mkdir()
    run(cwd, 'init')
    run(cwd, 'remote', 'add', 'origin', f'https://github.com/{project}.git')
    locks = PersonalTaskLocks(source.client)
    ticket = locks.claim(thread, project, None)
    sibling_ticket = locks.claim(sibling, project, None)

    approvals = Approvals()
    rpc = SharedCodex.__new__(SharedCodex)
    rpc.approvals = approvals
    rpc.approval_coordination = lambda: (source.client, True)
    rpc.call = Mock(return_value={'thread': {'cwd': str(cwd)}})
    rpc.closed = False
    rpc.write_lock = threading.Lock()
    rpc.p = SimpleNamespace(stdin=Mock())
    method = 'item/commandExecution/requestApproval'
    first, second, third = [approvals.add(rpc, {'id': index, 'method': method,
                            'params': {'threadId': thread, 'command': f'echo fixture {index}'}})
                            for index in (1, 2, 3)]
    assert approvals.decide_pending(first, True) == 'allowed'
    sender = CodexWorkspace(approvals)
    peer = CodexWorkspace(Approvals())
    outsider = CodexWorkspace(Approvals())
    sender.set_identity((source.client.url, source.client.device, source.client.token))
    sender.complete({'threadId': thread, 'turnId': 'fixture-complete',
                     'time': time.time(), 'title': 'Fixture'})
    sender.sync(source)
    assert outsider.sync(other) == [] and not outsider.pending()
    http_error(lambda: other.read('codex-live/' + source.client.device), 404)
    events = peer.sync(viewer)
    assert [event['turnId'] for event in events] == ['fixture-complete']
    selected = next(item['token'] for item in peer.pending() if item['sourceToken'] == second)
    assert peer.decide(selected, True) == 'queued' and rpc.p.stdin.write.call_count == 1
    peer.sync(viewer)
    sender.sync(source)
    peer.sync(viewer)
    assert peer.decisions()[selected] == 'allowed'
    refused = peer.pending()
    assert [item['sourceToken'] for item in refused] == [third]
    checkpoint = '1' * 64
    locks.handoff(thread, ticket, checkpoint)
    assert locks.request()['projects'][project] == source.client.device
    assert approvals.decide_pending(third, True) == 'control_lost'
    assert peer.decide(refused[0]['token'], True) == 'queued'
    peer.sync(viewer)
    sender.sync(source)
    peer.sync(viewer)
    assert peer.decisions()[refused[0]['token']] == 'control_lost'
    assert locks.request('/' + thread) == {'device': None, 'checkpoint': checkpoint}
    locks.handoff(sibling, sibling_ticket, checkpoint)
    PersonalTaskLocks(viewer.client).claim(thread, project, lambda _: checkpoint)
    assert approvals.decide_pending(third, True) == 'control_lost'
    responses = [json.loads(call.args[0]) for call in rpc.p.stdin.write.call_args_list]
    assert responses == [{'id': value, 'result': {'decision': 'accept'}} for value in (1, 2)]


def automatic_pairing(root, url, cert, record, payload):
    command = admin(root, "setup", "first", "--url", url, "--certificate", str(cert),
                    "--database", str(root / "coordination.sqlite3"))
    assert command.returncode == 0, command.stderr
    setup = command.stdout.strip()
    access, secret = decode_invite(setup, prefix=SETUP_PREFIX)
    configs = [root / name for name in ("setup-first", "setup-second", "setup-extra")]
    folders = [root / name for name in ("setup-projects-a", "setup-projects-b")]
    for path in [*configs, *folders]:
        path.mkdir()
    waiting = threading.Event()
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(join, configs[0], setup, folders[0], waiting=waiting.set, cancel=cancel)
        try:
            assert waiting.wait(8) and not first.done()
            assert not (configs[0] / "local-config.json").exists()
            pending = json.loads((configs[0] / ".local/pending-cloud-pairing.json").read_bytes())
            client = ControlClient({**access, "device": pending["device"], "projects": {}}, device_token(pending))
            http_error(RemoteRecords(client).index, 403)
            claim = {"code": secret, "device": client.device, "token": client.token}
            assert post(client, "join-setup", claim)["state"] == "waiting"
            assert join(configs[1], setup, folders[1])
            assert first.result(timeout=8)
        finally:
            cancel.set()
    paired = [json.loads((path / "local-config.json").read_bytes())["coordination"] for path in configs[:2]]
    assert paired[0]["device"] != paired[1]["device"]
    assert device_token(paired[0]) != device_token(paired[1])
    for coordination in paired:
        assert RemoteRecords(ControlClient(coordination, device_token(coordination))).read(record) == payload
    try:
        join(configs[2], setup, folders[1])
    except ValueError as error:
        assert "已用于两台电脑" in str(error)
    else:
        raise AssertionError("A third device joined a completed setup")
    with closing(sqlite3.connect(root / "coordination.sqlite3")) as db, db:
        db.execute("UPDATE device_setups SET expires=0")
    try:
        join(configs[2], setup, folders[1])
    except ValueError as error:
        assert "已过期" in str(error)
    else:
        raise AssertionError("An expired setup was accepted")
    return paired


def workspace_devices(root, url, cert, record, payload):
    database = str(root / "coordination.sqlite3")
    command = admin(root, "workspace-grant", "first", "--url", url, "--certificate", str(cert),
                    "--database", database)
    assert command.returncode == 0, command.stderr
    grant = command.stdout.strip()
    check_private_downloader(root, grant)
    profiles = [root / f"private-edition-{index}" for index in range(4)]

    def connect(profile):
        assert join_workspace(profile, grant)
        config = json.loads((profile / "local-config.json").read_bytes())
        assert "LANXI3." not in json.dumps(config)
        assert not (profile / "project-library.json").exists()
        return config["coordination"]

    first = connect(profiles[0])
    assert RemoteRecords(ControlClient(first, device_token(first))).read(record) == payload
    with ThreadPoolExecutor(max_workers=3) as workers:
        members = [first, *workers.map(connect, profiles[1:])]
    assert len({member["device"] for member in members}) == len(members)
    assert len({device_token(member) for member in members}) == len(members)
    assert connect(profiles[0]) == first
    for member in members:
        assert RemoteRecords(ControlClient(member, device_token(member))).read(record) == payload
    access, code = decode_invite(grant, prefix=WORKSPACE_PREFIX)
    new_client = ControlClient({**access, "device": "lanxi-" + "f" * 24, "projects": {}}, None)
    invalid = {"code": "A" * 43, "device": new_client.device, "token": "B" * 43}
    for proof in (invalid, {**invalid, "code": code}):
        if proof is not invalid:
            revoked = admin(root, "workspace-revoke", "first", "--database", database)
            assert revoked.returncode == 0, revoked.stderr
        try:
            post(new_client, "join-workspace", proof)
        except ValueError as error:
            assert "授权不可用" in str(error)
        else:
            raise AssertionError("An invalid or revoked workspace grant was accepted")
    return members


def check_private_downloader(root, grant):
    from desktop_storage import PROFILE, prepare_data
    from scripts.build_private_downloader import write_downloader

    folder = root / "私人下载"
    script = folder / "下载澜汐最新版.cmd"
    write_downloader(grant, script)
    profile = folder / "新用户"
    config = profile / "Lanxi/data/local-config.json"
    archive = folder / "Lanxi-R999-Windows-Private.zip"
    env = {**os.environ, "LOCALAPPDATA": str(profile), "LANXI_DEVICE_TOKEN": ""}

    def download():
        result = subprocess.run(["cmd.exe", "/d", "/c", "call", script.name, "--no-pause"],
                                cwd=folder, env=env, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stderr.decode("oem", errors="replace")

    download()
    assert archive.read_bytes() == (root / "releases" / archive.name).read_bytes()
    settings = json.loads(config.read_bytes())
    assert device_token(settings["coordination"]) not in config.read_text(encoding="utf-8")
    config_bytes = config.read_bytes()
    desktop_profile = config.with_name(PROFILE)
    def desktop_workspace():
        return prepare_data(folder, config.parent, lambda _: None)

    assert desktop_workspace() == config.parent.parent
    desktop_profile.unlink()  # An earlier downloader paired successfully but omitted the profile.
    archive_time = archive.stat().st_mtime_ns
    download()
    assert config.read_bytes() == config_bytes
    assert desktop_workspace() == config.parent.parent and archive.stat().st_mtime_ns == archive_time

    migrated = json.dumps({"version": 1, "workspace": str(root), "importedFrom": str(folder)}).encode()
    desktop_profile.write_bytes(migrated)
    download()
    assert desktop_profile.read_bytes() == migrated and desktop_workspace() == root

    env["LOCALAPPDATA"] = str(folder / "本地使用过的用户")
    previous = Path(env["LOCALAPPDATA"]) / "Lanxi/data/local-config.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"desktopWallpaperMinutes":15,"personalSync":{"interval":45}}', encoding="utf-8")
    download()
    updated = json.loads(previous.read_bytes())
    assert updated["desktopWallpaperMinutes"] == 15 and updated["personalSync"]["interval"] == 45
    assert updated["coordination"]["device"] != settings["coordination"]["device"]


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
    automatic = []
    workspace = []
    releases = root / "releases"
    releases.mkdir()
    archive = releases / "Lanxi-R999-Windows-Private.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("Lanxi-Glass-R999-Portable.exe", b"update fixture")
    manifest = {"version": "999", "size": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (releases / "latest.json").write_text(json.dumps(manifest))
    scope = releases / "Lanxi-R999-Windows-Private.scope"

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
            codex_activity_roundtrip(root, a, b, other)
            http_error(lambda: endpoint(a.client, "/999"), 404)
            scope.write_text(hashlib.sha256(devices["first"]["personalSpace"].encode()).hexdigest(), encoding="ascii")
            for suffix in ("", "/999"):
                http_error(lambda: endpoint(other.client, suffix), 403)
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
                    assert latest(saved) == manifest
                    try:
                        prepare(saved, {**manifest, "sha256": "0" * 64}, root / "versions")
                    except ValueError as error:
                        assert "校验失败" in str(error)
                    else:
                        raise AssertionError("Updater accepted a checksum mismatch")
                    update = prepare(saved, manifest, root / "versions")
                    assert update.read_bytes() == b"update fixture"
                    assert json.loads((laptop / "local-config.json").read_bytes()) == saved
                    duplicate = root / "different-device"
                    duplicate.mkdir()
                    try:
                        join(duplicate, invitation, folder)
                    except ValueError as error:
                        assert "已过期或已使用" in str(error)
                    else:
                        raise AssertionError("A second device reused a spent invitation")
                    automatic = automatic_pairing(root, url, cert, key_id, payload)
                    workspace = workspace_devices(root, url, cert, key_id, payload)
        with serve(root, port) as url:
            assert client(url, "second").read(key_id) == payload
            if paired:
                assert RemoteRecords(ControlClient(paired, device_token(paired))).read(key_id) == payload
            for member in automatic + workspace:
                assert RemoteRecords(ControlClient(member, device_token(member))).read(key_id) == payload
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "https": True,
        "device_isolation": True,
        "codex_activity_workspace_roundtrip": True,
        "codex_approval_project_control": True,
        "conflict_preserved": True,
        "persisted_after_restart": True,
        "accepted_bytes": len(large),
        "oversize_http": 413,
        "windows_clients_roundtrip": os.name == "nt",
        "windows_pairing_restart_persistence": paired is not None,
        "automatic_pairing_restart_persistence": len(automatic) == 2,
        "private_workspace_devices": len(workspace),
        "private_update_checksum_and_staging": paired is not None,
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
