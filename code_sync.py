"""Synchronize a registered GitHub branch without transferring large Git or LFS objects."""

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from personal_memory import safe_path, windows_reserved

LIMIT = 100 * 1024 * 1024
CODE_EXTENSIONS = {
    "py",
    "pyi",
    "ipynb",
    "c",
    "h",
    "cpp",
    "hpp",
    "cc",
    "cxx",
    "cs",
    "java",
    "kt",
    "kts",
    "swift",
    "rs",
    "go",
    "js",
    "jsx",
    "ts",
    "tsx",
    "vue",
    "svelte",
    "css",
    "scss",
    "html",
    "xml",
    "json",
    "toml",
    "yaml",
    "yml",
    "md",
    "rst",
    "txt",
    "cmake",
    "gradle",
    "sh",
    "bash",
    "ps1",
    "cmd",
    "bat",
    "rb",
    "php",
    "lua",
    "sql",
    "proto",
    "dart",
    "m",
    "mm",
    "r",
    "jl",
    "scala",
    "fs",
    "fsx",
    "ino",
    "cu",
    "cuh",
    "asm",
    "sln",
    "csproj",
    "vcxproj",
    "props",
    "targets",
    "lock",
    "mod",
    "sum",
    "gitignore",
    "gitattributes",
    "editorconfig",
    "zig",
    "nim",
    "ex",
    "exs",
    "erl",
    "hrl",
    "hs",
    "ml",
    "mli",
    "clj",
    "cljs",
    "groovy",
    "tf",
    "tfvars",
    "hcl",
    "v",
    "vh",
    "sv",
    "vhd",
    "f",
    "f90",
    "f95",
    "s",
    "sass",
    "less",
    "gql",
    "graphql",
    "tsconfig",
    "inc",
    "tpl",
    "bazel",
    "bzl",
    "in",
}
CODE_NAMES = {"dockerfile", "makefile", "cmakelists.txt", "license", "readme", "jenkinsfile"}
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class SyncIssue(ValueError):
    pass


def github_id(remote):
    if not isinstance(remote, str):
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+)/?",
        remote,
        re.IGNORECASE,
    )
    if not match:
        return None
    parts = match[1], match[2].removesuffix(".git")
    if not all(
        IDENT.fullmatch(p) and not p.startswith(".") and not p.endswith(".") and not windows_reserved(p)
        for p in parts
    ):
        return None
    return "/".join(parts).lower()


def run(repo, *args, data=None, check=True, trace=False, timeout=90):
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        )
    }
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="Never",
        GIT_NO_LAZY_FETCH="1",
        GIT_LFS_SKIP_SMUDGE="1",
        LC_ALL="C",
        GIT_LITERAL_PATHSPECS="1",
    )
    if trace:
        env["GIT_TRACE_PACKET"] = "1"
    cmd = [
        "git",
        "-c",
        "protocol.version=2",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "push.followTags=false",
    ]
    if repo:
        cmd += ["-C", str(repo)]
    result = subprocess.run(
        cmd + list(args),
        input=data or b"",
        check=False,
        capture_output=True,
        timeout=timeout,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and result.returncode:
        stderr = result.stderr.lower()
        if any(message in stderr for message in (
            b"user interactivity has been disabled", b"unable to get password", b"could not read username",
            b"authentication failed", b"permission denied (publickey)", b"repository not found",
        )):
            raise SyncIssue("GitHub 登录或仓库权限不可用，请在本机确认 Git 凭据及仓库访问权限")
        if b"dubious ownership" in stderr or b"unsafe repository" in stderr:
            raise SyncIssue("Git 拒绝访问该目录，请核对仓库所有者及 safe.directory 设置")
        if b"not a git repository" in stderr or b"invalid gitfile" in stderr:
            raise SyncIssue("本机 Git 工作区不可用，请核对项目路径和 Git 元数据")
        raise SyncIssue(f"Git {args[0]} 未完成，请核对网络、凭据和仓库状态")
    return result


def value(repo, *args):
    return run(repo, *args).stdout.decode().strip()


def remote_tip(repo, remote, branch):
    result = run(
        repo,
        "ls-remote",
        "--symref",
        remote,
        "HEAD",
        "refs/heads/" + branch if branch else "HEAD",
        trace=True,
    )
    if not re.search(rb"< fetch=[^\r\n]*\bfilter\b", result.stderr):
        raise SyncIssue("远端未声明支持按大小过滤，已暂停下载")
    refs = {}
    for line in result.stdout.decode().splitlines():
        left, right = line.split("\t", 1)
        if left.startswith("ref: refs/heads/") and right == "HEAD" and not branch:
            branch = left.removeprefix("ref: refs/heads/")
        else:
            refs[right] = left
    if (
        not branch
        or branch.startswith("-")
        or run(repo, "check-ref-format", "refs/heads/" + branch, check=False).returncode
    ):
        raise SyncIssue("远端分支无效")
    tip = refs.get("refs/heads/" + branch) or (
        refs.get("HEAD") if f"ref: refs/heads/{branch}\tHEAD" in result.stdout.decode().splitlines() else None
    )
    if not tip:
        raise SyncIssue("同步分支在远端不存在")
    return branch, tip


def entries(repo, revision):
    tree = {}
    for entry in run(repo, "ls-tree", "-rz", revision).stdout.split(b"\0"):
        if entry:
            header, raw = entry.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            tree[raw.decode("utf-8")] = (mode, kind, oid)
    return tree


def sizes(repo, oids):
    oids = sorted(set(oids))
    if not oids:
        return {}
    result = run(
        repo,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        data=("\n".join(oids) + "\n").encode(),
    ).stdout
    return {
        oid: (None if info == ["missing"] else int(info[1]))
        for oid, *info in (line.split() for line in result.decode().splitlines())
    }


def is_large(size):
    return size is None or size >= LIMIT


def sparse(repo, current, incoming, fresh=False):
    before = {} if fresh else entries(repo, current)
    after = entries(repo, incoming)
    blobs = sizes(repo, (oid for tree in (before, after) for _, kind, oid in tree.values() if kind == "blob"))
    excluded = []
    expand_after_merge = False
    for name in before.keys() | after.keys():
        old, new = before.get(name), after.get(name)
        old_big = old and old[1] == "blob" and is_large(blobs[old[2]])
        new_big = new and new[1] == "blob" and is_large(blobs[new[2]])
        if not (old_big or new_big):
            continue
        present = (repo / name).exists() or (repo / name).is_symlink()
        if present and not fresh and (old_big or not old):
            if old != new:
                raise SyncIssue("远端涉及本机已有的大文件，保留工作区等待处理")
        else:
            if old_big and new and not new_big:
                expand_after_merge = True
            if "\n" in name or "\r" in name:
                raise SyncIssue("大文件路径无法表示为稀疏检出规则")
            excluded.append(name)
    marker = run(repo, "config", "--bool", "lanxi.sparse", check=False).stdout.strip() == b"true"
    enabled = run(repo, "config", "--bool", "core.sparseCheckout", check=False).stdout.strip() == b"true"
    if enabled and not marker:
        raise SyncIssue("保留用户已有的稀疏检出配置")
    if excluded or marker:
        patterns = ["/*"] + ["!/" + re.sub(r"([\\*?\[\] ])", r"\\\1", n) for n in sorted(excluded)]
        run(
            repo, "sparse-checkout", "set", "--no-cone", "--stdin", data=("\n".join(patterns) + "\n").encode()
        )
        run(repo, "config", "lanxi.sparse", "true")
    return expand_after_merge


PRIVATE_DIRECTORIES = {".git", ".ssh", ".aws", ".azure", ".local", ".lanxi", ".codex", ".gnupg", ".kube", ".docker"}
GENERATED_DIRECTORIES = {
    ".cache", ".gradle", ".idea", ".vs", ".next", ".nuxt", ".pytest_cache", ".tox", ".mypy_cache",
    ".ruff_cache", ".terraform", "node_modules", ".venv", "venv", "dist", "build", "bin", "obj",
    "target", "coverage", "out", "release", "releases", "build-exe", "__pycache__",
}
PRIVATE_NAMES = {
    ".npmrc", ".netrc", ".git-credentials", "id_rsa", "id_ed25519", ".pypirc", ".dockercfg",
    "token", "tokens", "password", "passwords", "secret", "apikey", "api_key", "service-account",
    "service_account", "serviceaccount", "client_secret", "client_secrets", "kubeconfig",
}


def auto_path(name, tracked, path):
    parts = Path(name)
    low = parts.name.lower()
    if (
        any(
            p.casefold() in PRIVATE_DIRECTORIES or p.casefold() in GENERATED_DIRECTORIES
            or p.casefold() in PRIVATE_NAMES or p.casefold().split(".", 1)[0] in PRIVATE_NAMES
            for p in parts.parts
        )
        or low.startswith(
            (".env", "credentials", "secrets", "auth.json", "local-config.", "personal-sync.json")
        )
        or low.endswith((".key", ".pem", ".pfx", ".p12"))
    ):
        return False
    if tracked or low in CODE_NAMES or low.rsplit(".", 1)[-1] in CODE_EXTENSIONS:
        return True
    if parts.suffix.lower() in {".csv", ".tsv", ".log", ".jsonl", ".ndjson", ".sqlite", ".sqlite3", ".db"}:
        return False
    # New languages, extensionless scripts and project configuration are often plain text.
    with path.open("rb") as stream:
        prefix = stream.read(8192)
    return bool(prefix) and not any(c < 32 and c not in (9, 10, 12, 13) for c in prefix)


def commit_saved(repo, memory_subdir):
    raw = run(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--no-renames",
        "--untracked-files=all",
        "--ignore-submodules=all",
    ).stdout
    names, new = [], []
    for row in raw.split(b"\0"):
        if not row:
            continue
        state, name = row[:2], row[3:].decode()
        if state != b"??" and state[:1] != b" ":
            raise SyncIssue("等待本机暂存或冲突处理完成")
        if name.casefold().startswith(memory_subdir.casefold() + "/"):
            continue
        path = repo / name
        if state != b" D":
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or is_large(info.st_size)
            ):
                continue
        if not auto_path(name, state != b"??", path):
            continue
        names.append(row[3:])
        if state == b"??":
            new.append(row[3:])
    if not names:
        return False
    attrs = run(repo, "check-attr", "-z", "--stdin", "filter", data=b"\0".join(names) + b"\0").stdout.split(
        b"\0"
    )
    allowed = {
        p for p, attr, v in zip(attrs[::3], attrs[1::3], attrs[2::3]) if v in (b"unspecified", b"unset")
    }
    names = [p for p in names if p in allowed]
    new = [p for p in new if p in allowed]
    if new:
        run(
            repo,
            "add",
            "--intent-to-add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            data=b"\0".join(new) + b"\0",
        )
    if names:
        run(
            repo,
            "commit",
            "--only",
            "-m",
            "WIP: 澜汐同步已保存代码",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            data=b"\0".join(names) + b"\0",
        )
    return bool(names)


def verify_checkout(repo, identity):
    if (
        not (repo / ".git").exists()
        or repo.resolve() != Path(value(repo, "rev-parse", "--show-toplevel")).resolve()
    ):
        raise SyncIssue("本机目录已存在，且不是登记的仓库")
    origin = run(repo, "config", "--get", "remote.origin.url", check=False).stdout.decode().strip()
    if not origin:
        raise SyncIssue("本机仓库未配置 origin")
    if github_id(origin) != identity and github_id(value(repo, "remote", "get-url", "origin")) != identity:
        raise SyncIssue("本机 origin 与登记的项目不一致")


def finish_placeholder(repo, identity, marker_name):
    marker = Path(value(repo, "rev-parse", "--absolute-git-dir")) / marker_name
    if marker.read_text(encoding="utf-8") != identity:
        raise SyncIssue("占位项目检出记录与仓库不一致")
    missing = run(repo, "ls-files", "--deleted", "-z").stdout
    if missing:
        run(repo, "checkout-index", "-z", "--stdin", data=missing)
    marker.unlink()
    return "已克隆"


def sync_project(repo, remote, branch=None, *, memory_subdir=".codex/memories", placeholder_id=None):
    identity = github_id(remote)
    if not identity:
        raise SyncIssue("仅接受已登记的 GitHub 仓库")
    repo = Path(repo)
    if (
        repo.is_symlink()
        or repo.exists()
        and getattr(repo.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise SyncIssue("本机项目位置为目录链接")
    from plain_projects import receiver_placeholder
    placeholder = bool(placeholder_id and receiver_placeholder(repo, placeholder_id))
    pending_checkout = "lanxi-placeholder-checkout"
    if not repo.exists() or repo.is_dir() and not any(repo.iterdir()) or placeholder:
        selected, _ = remote_tip(None, remote, branch)
        repo.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=repo.parent, prefix=".lanxi-clone-") as temp:
            stage = Path(temp) / "work"
            run(
                None,
                "clone",
                "--filter=blob:limit=" + str(LIMIT),
                "--no-checkout",
                "--no-tags",
                "--single-branch",
                "--branch",
                selected,
                "--",
                remote,
                str(stage),
                timeout=900,
            )
            sparse(stage, None, "HEAD", fresh=True)
            run(stage, "read-tree", "-mu", "HEAD")
            exclude_memory(stage, memory_subdir)
            run(stage, "config", "lanxi.blobLimit", str(LIMIT))
            if placeholder:
                if run(stage, "ls-tree", "--name-only", "HEAD", "--", ".lanxi").stdout or not receiver_placeholder(repo, placeholder_id):
                    raise SyncIssue("普通项目占位目录已变化，保留现有文件")
                exclude_memory(stage, ".lanxi")
                # Publish Git metadata first. Only restore missing tracked paths; a
                # retry completes an interrupted checkout without replacing user data.
                (stage / ".git" / pending_checkout).write_text(identity, encoding="utf-8")
                (stage / ".git").rename(repo / ".git")
                return finish_placeholder(repo, identity, pending_checkout)
            existed = repo.is_dir()
            if existed:
                repo.rmdir()  # Succeeds only while the target is still empty.
            try:
                stage.rename(repo)
            except OSError:
                if existed:
                    repo.mkdir(exist_ok=True)
                raise
        return "已克隆"
    verify_checkout(repo, identity)
    gitdir = Path(value(repo, "rev-parse", "--absolute-git-dir"))
    if (gitdir / pending_checkout).is_file():
        return finish_placeholder(repo, identity, pending_checkout)
    exclude_memory(repo, memory_subdir)
    if any(
        (gitdir / x).exists()
        for x in (
            "index.lock",
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-apply",
            "rebase-merge",
            "BISECT_START",
            "lanxi-switch/active.json",
        )
    ):
        raise SyncIssue("等待当前 Git 操作结束")
    local = run(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.decode().strip()
    if not local:
        raise SyncIssue("本机未检出分支，请切回登记分支")
    selected, tip = remote_tip(repo, "origin", branch)
    if selected != local:
        raise SyncIssue("本机处于其他分支，保留当前工作区")
    tracking = "refs/remotes/origin/" + selected
    cached = run(repo, "rev-parse", "--verify", tracking, check=False).stdout.decode().strip()
    managed_sparse = run(repo, "config", "--bool", "lanxi.sparse", check=False).stdout.strip() == b"true"
    saved_limit = run(repo, "config", "--get", "lanxi.blobLimit", check=False).stdout.strip()
    refetch = managed_sparse and saved_limit != str(LIMIT).encode()
    if tip != cached or refetch:
        run(
            repo,
            "fetch",
            "--no-tags",
            *(("--refetch",) if refetch else ()),
            "--filter=blob:limit=" + str(LIMIT),
            "origin",
            "refs/heads/" + selected + ":" + tracking,
            timeout=900,
        )
    commit_saved(repo, memory_subdir)
    head = value(repo, "rev-parse", "HEAD")
    if refetch:
        sparse(repo, head, head)
        run(repo, "config", "lanxi.blobLimit", str(LIMIT))
    if head == tip:
        return "已同步"
    if run(repo, "merge-base", "--is-ancestor", tracking, head, check=False).returncode:
        target = tracking
        if run(repo, "merge-base", "--is-ancestor", head, tracking, check=False).returncode:
            merged = run(repo, "merge-tree", "--write-tree", head, tracking, check=False)
            if merged.returncode:
                raise SyncIssue("两端修改发生冲突，双方提交均已保留")
            target = value(
                repo,
                "commit-tree",
                merged.stdout.decode().splitlines()[0],
                "-p",
                head,
                "-p",
                tracking,
                "-m",
                "合并两台设备的已保存代码",
            )
        expand = sparse(repo, head, target)
        run(repo, "merge", "--ff-only", "--no-stat", "--no-overwrite-ignore", target)
        if expand:
            sparse(repo, target, target)
    head = value(repo, "rev-parse", "HEAD")
    if head != tip:
        configured = (
            run(repo, "config", "--get-all", "remote.origin.pushurl", check=False)
            .stdout.decode()
            .splitlines()
        )
        if any(github_id(url) != identity for url in configured):
            raise SyncIssue("推送地址与登记的 GitHub 仓库不一致")
        objects = (
            run(repo, "rev-list", "--objects", "--no-object-names", tracking + ".." + head)
            .stdout.decode()
            .splitlines()
        )
        if any(is_large(n) for n in sizes(repo, objects).values()):
            raise SyncIssue("待推送历史含超限对象，已保留本地提交")
        run(repo, "push", "--no-follow-tags", "origin", head + ":refs/heads/" + selected)
    return "已同步"


def exclude_memory(repo, subdir):
    # Other tools (including scheduled backups) also honor this local exclusion.
    safe_path(repo, subdir)
    path = Path(value(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    rule = ("/" + re.sub(r"([\\*?\[\] ])", r"\\\1", subdir) + "/").encode()
    if path.is_file() and rule in path.read_bytes().splitlines():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as output:
        output.write(b"\n" + rule + b"\n")
