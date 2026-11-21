"""Enroll a source desktop's ordinary project in a newly created private repository."""

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from code_sync import (GENERATED_DIRECTORIES, HTTP_IDLE_OPTIONS, PRIVATE_DIRECTORIES, SyncIssue,
                       auto_path, github_id, is_large, run, value)
from handoff_sync import atomic_write
from personal_memory import safe_path
from service_lock import ServiceLock

MARKER = "lanxi-private-project.json"


def _read(folder):
    if (folder / ".git").is_dir() and (folder / ".git").resolve() == folder / ".git":
        marker = folder / ".git" / MARKER
        if marker.is_file() and marker.stat().st_size < 4096 and marker.resolve() == marker:
            state = json.loads(marker.read_bytes())
            if isinstance(state, dict) and str(uuid.UUID(state["nonce"])) == state["nonce"]:
                return state
    return None


def managed(folder):
    try:
        return _read(Path(folder)) is not None
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False


def complete(folder):
    state = _read(Path(folder))
    return bool(state and state.get("complete") is True)


def _save(directory, state):
    atomic_write(directory / MARKER, json.dumps(state).encode())


def _initialize(folder):
    with tempfile.TemporaryDirectory(prefix=".lanxi-init-", dir=folder.parent) as name:
        scratch = Path(name)
        run(None, "init", "--template=", "-b", "main", str(scratch))
        admin = scratch / ".git"
        (admin / "info").mkdir(exist_ok=True)
        ignored = PRIVATE_DIRECTORIES | GENERATED_DIRECTORIES
        (admin / "info/exclude").write_text("".join(p + "/\n" for p in sorted(ignored)), encoding="utf-8")
        _save(admin, {"nonce": str(uuid.uuid4())})
        if os.path.lexists(folder / ".git"):
            raise SyncIssue("项目已出现 Git 元数据，请重新核对")
        admin.rename(folder / ".git")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _api(token, endpoint, data=None):
    request = Request("https://api.github.com" + endpoint,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json", "User-Agent": "Lanxi-private-projects"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            result = json.loads(response.read(256 * 1024))
            if not isinstance(result, dict):
                raise SyncIssue("GitHub 仓库信息格式异常")
            return result
    except HTTPError as error:
        with closing(error):
            if error.code == 404 and data is None and endpoint.startswith("/repos/"):
                return None
            raise SyncIssue(f"GitHub 返回 HTTP {error.code}，请检查登录及私有仓库权限") from None
    except (URLError, OSError, ValueError):
        raise SyncIssue("未能核对 GitHub 私有仓库，请稍后重试") from None


def _token():
    gh = shutil.which("gh")
    if gh:
        try:
            result = subprocess.run([gh, "auth", "token", "--hostname", "github.com"],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and re.fullmatch(rb"[!-~]+", result.stdout.strip()):
                return result.stdout.strip().decode("ascii")
        except (OSError, subprocess.TimeoutExpired):
            pass
    credential = run(None, "credential", "fill", data=b"protocol=https\nhost=github.com\n\n",
                     check=False, timeout=15)
    fields = dict(line.split(b"=", 1) for line in credential.stdout.splitlines() if b"=" in line)
    if (credential.returncode == 0 and fields.get(b"protocol") == b"https"
            and fields.get(b"host") == b"github.com" and re.fullmatch(rb"[!-~]+", fields.get(b"password", b""))):
        return fields[b"password"].decode("ascii")
    raise SyncIssue("尚未取得 github.com 登录凭据，请先用 GitHub CLI 或 Git 凭据管理器登录")


def _identity(login, name):
    url = f"https://github.com/{login}/{name}.git"
    if github_id(url) != f"{login}/{name}".lower() or name.lower().endswith(".git"):
        raise SyncIssue("项目名称或 GitHub 账户不能用于自动创建仓库")
    return url


def _private_repo(folder, token, state, login):
    description = "Lanxi private project " + state["nonce"]
    endpoint = "/repos/" + login + "/" + folder.name
    repo = _api(token, endpoint)
    if repo is None:
        if "repo" in state:
            raise SyncIssue("已登记的 GitHub 仓库不可访问，请核对仓库及账号")
        repo = _api(token, "/user/repos", {"name": folder.name, "private": True,
                                           "auto_init": False, "description": description})
    identity = (login + "/" + folder.name).casefold()
    if (not isinstance(repo.get("id"), int) or repo.get("private") is not True
            or str(repo.get("full_name", "")).casefold() != identity
            or (repo["id"] != state["repo"] if "repo" in state else repo.get("description") != description)):
        raise SyncIssue("GitHub 有同名仓库，无法确认属于这个项目，请核对后手动接入")
    state.update(repo=repo["id"], owner=login)
    _save(folder / ".git", state)


def _selection(folder):
    raw = run(folder, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
    names = []
    for item in dict.fromkeys(raw.split(b"\0")):
        if not item or item.endswith(b"/"):
            continue
        name = item.decode("utf-8")
        try:
            path = safe_path(folder, name)
            info = path.lstat()
        except (OSError, ValueError):
            continue
        if (not stat.S_ISREG(info.st_mode) or is_large(info.st_size) or path.name.casefold() == ".gitmodules"
                or not auto_path(name, False, path)):
            continue
        names.append(item)
    if not names:
        return []
    attrs = run(folder, "check-attr", "-z", "--stdin", "filter", data=b"\0".join(names) + b"\0").stdout
    fields = attrs.split(b"\0")
    return [path for path, mode in zip(fields[::3], fields[2::3]) if mode in (b"unset", b"unspecified")]


def _initial_commit(folder, state):
    title = "Lanxi initial project " + state["nonce"]
    head = run(folder, "rev-parse", "--verify", "HEAD", check=False)
    if value(folder, "symbolic-ref", "--short", "HEAD") != "main":
        raise SyncIssue("本机正在使用其他分支，请核对后手动接入")
    if head.returncode:
        names = _selection(folder)
        staged = set(run(folder, "ls-files", "--cached", "-z").stdout.split(b"\0")) - {b""}
        if staged.difference(names):
            raise SyncIssue("本机暂存区包含筛选范围外的文件，请人工核对")
        if names:
            run(folder, "add", "--pathspec-from-file=-", "--pathspec-file-nul", data=b"\0".join(names) + b"\0")
        for setting, fallback in (("user.name", "Lanxi"), ("user.email", "noreply@lanxi.invalid")):
            if not run(folder, "config", "--get", setting, check=False).stdout.strip():
                run(folder, "config", "--local", setting, fallback)
        run(folder, "-c", "commit.gpgSign=false", "commit", "--allow-empty", "-m", title)
    elif value(folder, "log", "-1", "--format=%s", "HEAD") != title or value(folder, "log", "-1", "--format=%P", "HEAD"):
        raise SyncIssue("本机已有其他提交，请核对后手动接入")


def _origin(folder, url, *, add=False):
    remotes = value(folder, "remote").splitlines()
    if not remotes and add:
        run(folder, "remote", "add", "origin", url)
        remotes = ["origin"]
    if remotes and (remotes != ["origin"] or value(folder, "remote", "get-url", "--all", "origin") != url
                    or value(folder, "remote", "get-url", "--push", "--all", "origin") != url):
        raise SyncIssue("本机已有不同的 Git 远程地址，请核对后手动接入")


def _push(folder, url, token, login):
    encoded = base64.b64encode((login + ":" + token).encode()).decode("ascii")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("GIT_", "GCM_"))}
    env.update(LANXI_HTTP_AUTH="Authorization: Basic " + encoded, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    for setting in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
        if setting in os.environ:
            env[setting] = os.environ[setting]
    command = ["git", "-C", str(folder), *HTTP_IDLE_OPTIONS, "-c", "http.followRedirects=false", "-c", "credential.helper=",
               "-c", "core.hooksPath=" + os.devnull, "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no",
               "-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    options = dict(stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=90,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    target = subprocess.run(command + ["remote", "get-url", "--push", "--all", "origin"], **options)
    if target.returncode or target.stdout.strip() != url.encode("ascii"):
        raise SyncIssue("首次推送地址与已核对的 GitHub 私有仓库不符")
    result = subprocess.run(
        command + ["--config-env=http." + url + ".extraHeader=LANXI_HTTP_AUTH",
                   "push", "--set-upstream", "origin", "HEAD:refs/heads/main"], **options,
    )
    if result.returncode:
        raise SyncIssue("首次推送未完成，请核对网络、写入权限及远程分支，稍后可重试")


def promote(folder):
    folder = Path(folder)
    try:
        if (not folder.is_absolute() or not folder.is_dir() or folder.resolve() != folder
                or any(os.path.lexists(parent / ".git") for parent in folder.parents)
                or any((base / "HEAD").is_file() and (base / "objects").is_dir()
                       and (base / "refs").is_dir() for base in (folder, *folder.parents))):
            raise SyncIssue("不能自动接入嵌套、链接或无效目录")
        exists = os.path.lexists(folder / ".git")
        if exists and not managed(folder):
            raise SyncIssue("本机目录已有 Git 仓库，请核对后手动接入")
        if not exists:
            _initialize(folder)
        with closing(ServiceLock(folder / ".git" / "lanxi-private.lock")):
            state = _read(folder)
            if not state:
                raise SyncIssue("无法核对澜汐项目登记")
            if state.get("complete"):
                url = _identity(state["owner"], folder.name)
                if value(folder, "remote") != "origin":
                    raise SyncIssue("已接入的 origin 有变更，请核对")
                _origin(folder, url)
                return "已接入 GitHub"
            _initial_commit(folder, state)
            _identity("account", folder.name)
            token = _token()
            login = _api(token, "/user").get("login")
            if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
                raise SyncIssue("无法确认 GitHub 登录账户")
            if "owner" in state and login.casefold() != state["owner"].casefold():
                raise SyncIssue("GitHub 登录账户与首次接入时不同")
            url = _identity(login, folder.name)
            _origin(folder, url)
            _private_repo(folder, token, state, login)
            _origin(folder, url, add=True)
            _push(folder, url, token, login)
            state["complete"] = True
            _save(folder / ".git", state)
            return "已接入 GitHub"
    except SyncIssue:
        raise
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, RuntimeError, TypeError):
        raise SyncIssue("无法完成普通项目接入，请检查网络、Git 配置及目录状态") from None
