"""Issue a seven-day Windows downloader for one hash-verified private package."""

import argparse
import base64
import hashlib
import re
import shlex
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def issue(args):
    archive = args.archive.resolve(strict=True)
    known = [line for line in args.known_hosts.read_text().splitlines()
             if line.startswith(args.host + " ssh-ed25519 ")]
    if len(known) != 1 or not re.fullmatch(r"[a-zA-Z0-9.-]+", args.host):
        raise ValueError("Expected one verified ED25519 host key for the specified host")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+\.zip", archive.name):
        raise ValueError("Expected a portable ZIP with an ASCII filename")
    if args.output.exists():
        raise FileExistsError(args.output)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    directory = "/home/admin/lanxi-downloads/" + digest[:12]
    remote = directory + "/" + archive.name
    host = "admin@" + args.host
    options = ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "ConnectTimeout=15",
               "-o", f'UserKnownHostsFile="{args.known_hosts.resolve()}"', "-i", str(args.identity)]

    def ssh(command, **kwargs):
        return subprocess.run(["ssh", *options, host, command], check=True, **kwargs)

    ssh(f"umask 077 && mkdir -p {directory}")
    staging = directory + "/upload-" + uuid.uuid4().hex
    try:
        subprocess.run(["scp", *options, str(archive), host + ":" + staging], check=True)
        ssh(f"set -eu; chmod 600 {staging}; echo '{digest}  {staging}' | sha256sum -c -; "
            f"if ! test -e {remote}; then ln {staging} {remote}; fi; echo '{digest}  {remote}' | sha256sum -c -")
    finally:
        ssh(f"rm -f -- {staging}")

    now = int(ssh("date -u +%s", capture_output=True, text=True).stdout)
    expires = datetime.fromtimestamp(now, timezone.utc) + timedelta(days=7)
    # OpenSSH 8.0 parses authorized_keys expiry-time in the server's local time zone.
    server_expiry = ssh(f"date -d @{int(expires.timestamp())} +%Y%m%d%H%M%S",
                        capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r"[0-9]{14}", server_expiry):
        raise ValueError("Unexpected expiry time from SSH server")
    with tempfile.TemporaryDirectory() as temp:
        key = Path(temp) / "download"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        "lanxi-download-" + digest[:12], "-f", str(key)], check=True)
        public = key.with_suffix(".pub").read_text().strip()
        entry = (f'restrict,expiry-time="{server_expiry}",command="/usr/bin/cat -- {remote}" '
                 + public)
        replacements = {
            "__SERVER__": args.host,
            "__ACCOUNT__": "admin",
            "__KNOWN_HOST__": " ".join(known[0].split()[:3]),
            "__READ_ONLY_KEY__": base64.b64encode(key.read_bytes()).decode("ascii"),
            "__ARCHIVE__": archive.name,
            "__SHA256__": digest,
            "__EXPIRY_UTC__": expires.isoformat(timespec="seconds"),
        }
        template = Path(__file__).with_name("download-portable.cmd.in").read_text(encoding="utf-8")
        for marker, value in replacements.items():
            template = template.replace(marker, value)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\r\n") as stream:
            stream.write(template)
        ssh("set -eu; umask 077; cd ~/.ssh; "
            + "flock authorized_keys sh -c " + shlex.quote('printf "\\n%s\\n" "$1" >> authorized_keys')
            + " sh " + shlex.quote(entry))
    print(f"{args.output}\nSHA256 {digest}\nExpires {expires.isoformat()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--known-hosts", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    issue(parser.parse_args())
