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

import base64
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


# 单 commit 重建的提交标题前缀：既是对人类的说明，也是幂等判定标记——
# 远端固定分支 HEAD 的标题以此开头即视为「本工具生成的单 commit」
SYNC_COMMIT_PREFIX = "sync: 同步英文译稿"


def rebuild_branch(owner, repo, token, base_branch, head_branch, files,
                   dest_dir, log=print):
    """以目标分支为基点重建固定分支：全部变更一次写入、单个 commit、按需强推。

    背景：旧实现逐文件调 contents API 写入，一个 PR 动辄几百个 commit，
    且逐文件请求在限流下耗时极长。改为本地 git 批量写入后天然满足
    「一个 PR 一个 commit」：每次都从目标分支基点重建固定分支，写入
    全部变更译稿后只产生一个 commit，force push 覆盖远端固定分支
    （分支为工具独占，重建安全）。重建语义同时自愈了陈旧文件残留——
    基点是目标分支，不在源目录中的旧 added 文件自然消失，无需清理逻辑。

    幂等判定（避免每次运行都强推制造 PR 噪音）：
    - 新树 == 远端固定分支树，且远端 HEAD 标题带本工具前缀 → 不推送；
    - 树相同但远端是历史遗留的多 commit 分支（存量碎 commit）→ 仍强推，
      把 PR 历史强制压缩为单个 commit。

    Args:
        owner/repo: Fork 仓库（写入目标）
        base_branch: 目标分支名（重建基点，须已存在于 Fork）
        head_branch: 固定分支名 translator/auto-<目标分支>
        files: [{"target_path", "content_b64"}] 变更译稿清单
        dest_dir: 本地克隆目录（自动覆盖重建）

    Returns:
        dict: {"pushed": bool, "written": [{"path", "action"}...],
               "reason": str}
        written 的 action 为 create/update（按 git name-status 换算）。
    """
    # ── 1. 克隆 Fork：浅、全分支头、带 blob（重建工作树需要完整内容）──
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir)
    parent = os.path.dirname(dest_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)
    url = _auth_url(owner, repo, token)
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", "--no-checkout",
             "--no-single-branch", url, dest_dir],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise client.GitCodeError(
            200, f"克隆 Fork {owner}/{repo} 失败: "
                 f"{e.stderr.strip() or e.stdout.strip()}")

    # 禁止行尾/编码规范化：译稿按原始字节提交，避免 .gitattributes 意外改写
    _run_git(dest_dir, "config", "core.autocrlf", "false")
    attrs = os.path.join(dest_dir, ".git", "info", "attributes")
    with open(attrs, "w", encoding="utf-8") as fh:
        fh.write("* -text\n")

    # 提交者身份：Jenkins agent 未必有全局 git 配置（缺省会报
    # Author identity unknown），仓库级显式配置，作者即 Fork 账号
    _run_git(dest_dir, "config", "user.name", owner)
    _run_git(dest_dir, "config", "user.email",
             f"{owner}@noreply.gitcode.com")

    # ── 2. 以目标分支为基点检出固定分支（目标分支不在 Fork 上时失败）──
    log(f"正在重建固定分支（单 commit）: {head_branch}")
    _run_git(dest_dir, "checkout", "-B", head_branch,
             f"origin/{base_branch}")

    # ── 3. 记录远端固定分支现状（幂等与存量判定）──
    remote_ref = f"origin/{head_branch}"
    remote_exists = True
    try:
        remote_tree = _run_git(
            dest_dir, "rev-parse", remote_ref + "^{tree}").strip()
        remote_subject = _run_git(
            dest_dir, "log", "-1", "--format=%s", remote_ref).strip()
    except client.GitCodeError:
        remote_exists = False
        remote_tree = remote_subject = None

    # ── 4. 批量写入译稿（base64 解码后按原始字节落盘）──
    for f in files:
        path = os.path.join(dest_dir, f["target_path"])
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(f["content_b64"]))
    _run_git(dest_dir, "add", "-A")
    status = _run_git(dest_dir, "diff", "--cached", "--name-status")
    action_map = {"A": "create", "M": "update"}
    written = []
    for line in status.splitlines():
        fields = line.split("\t", 1)
        if len(fields) == 2 and fields[0] in action_map:
            written.append({"path": fields[1], "action": action_map[fields[0]]})

    # ── 5. 单个 commit（无变更时仅在需要压缩存量历史时生成空 commit）──
    if written:
        msg = f"{SYNC_COMMIT_PREFIX}（{len(written)} 个文件）"
        _run_git(dest_dir, "commit", "-m", msg)
    elif remote_exists and not remote_subject.startswith(SYNC_COMMIT_PREFIX):
        # 树与基点一致但远端是旧碎 commit 历史：空 commit 强推压缩历史
        _run_git(dest_dir, "commit", "--allow-empty", "-m", SYNC_COMMIT_PREFIX)
    else:
        return {"pushed": False, "written": [],
                "reason": "分支内容已是最新"}

    # ── 6. 幂等判定：树相同且远端已是本工具单 commit → 不推送 ──
    new_tree = _run_git(dest_dir, "rev-parse", "HEAD^{tree}").strip()
    if (remote_exists and new_tree == remote_tree
            and remote_subject.startswith(SYNC_COMMIT_PREFIX)):
        return {"pushed": False, "written": [],
                "reason": "远端已是最新单 commit"}

    # ── 7. 强推（重建历史必须 force；分支为工具独占，安全）──
    _run_git(dest_dir, "push", "--force", "origin", head_branch)
    log(f"已推送单 commit（{len(written)} 个文件变更）: {head_branch}")
    return {"pushed": True, "written": written, "reason": "重建完成"}


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
