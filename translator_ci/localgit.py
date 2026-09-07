"""本地 git 仓库集成：把远程仓库克隆到本地，用 git 命令做索引与读内容。

本地化背景：GitCode API 有单账号 50 次/分钟限流，而旧实现的目录索引、
文件列举、逐文件对比会发起上百次请求，必然触发 429（即使有进程级限流
也只能靠长时间等待硬扛）。改为在 agent 上用 git 协议浅克隆仓库后，用
git 命令本地完成索引与对比——git 协议不经过 API，不受该限流约束，且
一次 ls-tree 等价于旧实现数十次 list_dir 调用。API 只保留建 PR 等
少量写操作。

克隆策略：
- 源（译稿权威）仓库由 Jenkinsfile 克隆，用 --no-checkout 全量浅克隆，
  git cat-file 读内容完全本地、即时。
- 目标仓库由本模块克隆，用 --filter=blob:none 只拉提交与目录树（体积
  小、快），对比只需目录树，无需 blob；失败不拉取。

git 命令全部走 subprocess，不在进程内解析 git 对象库格式；失败统一
抛 client.GitCodeError，与 API 路径的错误口径一致。
"""

import os
import shutil
import subprocess

from translator_ci import client
from translator_ci.common import GIT_HOST


def _auth_url(owner, repo, token):
    """构造带凭据的 git clone 地址（oauth2 用户名对应 GitCode 个人访问令牌）。

    与既有 gitcode remote 的写法一致；token 出现在克隆命令里，由 Jenkins
    密码参数注入，构建日志中会被脱敏。
    """
    return f"https://oauth2:{token}@{GIT_HOST.lstrip('https://')}/{owner}/{repo}.git"


def clone(owner, repo, ref, token, dest_dir, log=print, blobs=False):
    """浅克隆远程仓库到 dest_dir（覆盖重建），返回本地目录路径。

    ref 非空时克隆指定分支（分支不存在即失败）；ref 为空时克隆默认分支。
    默认 --filter=blob:none 只拉提交/目录树，blob 在 git cat-file 时按需
    补取（promisor 远端沿用克隆地址的凭据）；blobs=True 时全量浅克隆，
    适合需要频繁本地读内容的源仓库。
    """
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    parent = os.path.dirname(dest_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = ["git", "clone", "--depth=1", "--no-checkout"]
    if not blobs:
        cmd.append("--filter=blob:none")
    if ref:
        cmd += ["--branch", ref]
    cmd += [_auth_url(owner, repo, token), dest_dir]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise client.GitCodeError(
            200, f"克隆 {owner}/{repo}@{ref or '默认分支'} 失败: "
                 f"{e.stderr.strip() or e.stdout.strip()}")
    return dest_dir


def tree_map(repo_dir, ref="HEAD"):
    """返回本地仓库 ref 下全部文件路径 → blob sha 的映射。

    用 git ls-tree -r 一次性递归列出（只含 blob，不含目录条目），
    等价于旧实现的逐目录 list_dir，但只占 1 次本地命令。
    """
    out = _run_git(repo_dir, "ls-tree", "-r", ref)
    result = {}
    for line in out.splitlines():
        # 格式: <mode> <type> <sha>\t<path>
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob":
            result[path] = fields[2]
    return result


def cat_blob(repo_dir, blob_sha):
    """读取本地仓库指定 blob 的原始字节（部分克隆时按需从远端补取）。"""
    return _run_git_bytes(repo_dir, "cat-file", "blob", blob_sha)


def fetch_commit(repo_dir, sha, token=None, log=print):
    """按需补取指定提交（增量对比需要 before 提交，浅克隆未包含）。

    不带凭据 fetch 公开仓库即可；token 保留参数位以备私有仓库需要。
    拉取失败（如 webhook 传来的 sha 已不可达）抛 GitCodeError，由调用方
    决定回退全量扫描。
    """
    _run_git(repo_dir, "fetch", "--depth=1", "origin", sha, "--quiet")
    return sha


def diff_paths(repo_dir, before, after):
    """列出 before..after 之间的全部变更路径（含新增/删除/修改/改名）。

    git diff --name-only 直接给路径集合；before 不可达（对象不存在）
    时 git 报错，由 _run_git 统一抛 GitCodeError，调用方回退全量。

    Returns:
        set[str]: 变更文件/目录路径集合（无变更返回空集合）。
    """
    out = _run_git(repo_dir, "diff", "--name-only", before, after)
    return {line for line in out.splitlines() if line.strip()}


def read_file(repo_dir, path):
    """读取本地仓库 HEAD 上指定路径的原始字节（文件不存在视为 404）。"""
    try:
        return _run_git_bytes(repo_dir, "show", f"HEAD:{path}")
    except client.GitCodeError:
        raise client.GitCodeError(404, f"本地仓库不存在文件: {path}")


def _run_git(repo_dir, *args):
    return _run_git_bytes(repo_dir, *args).decode("utf-8", errors="replace")


def _run_git_bytes(repo_dir, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", repo_dir, *args], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise client.GitCodeError(
            200, f"git {' '.join(args)} 失败: "
                 f"{e.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout
