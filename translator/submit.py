"""幂等提交：Fork 目标仓库、维护固定分支、写入文件并创建/复用 PR。

submit_repo: 单仓库幂等提交（识别用户/Fork → 校验目标分支 → 同步
             Fork 分支 → 以目标分支为基点重建固定分支
             translator/auto-<目标分支>（全部变更合并为单个 commit，
             由 localgit.rebuild_branch 完成）→ 去重后创建 PR）

幂等语义：同一仓库重复运行时，已开 PR 不重复创建，新变更以重建的
单 commit 覆盖固定分支；内容无变化时不强推（避免 PR 噪音）。历史
遗留的多 commit 固定分支会被强制压缩为单个 commit。重建以目标分支
为基点，源目录中已消失的旧文件自然不在新树中，无需清理逻辑。
无新变更且无未合并 PR 时，回传该固定分支上的历史 PR（优先已合并），
让调用方仍能拿到 PR 链接做合并状态判定。
"""

import os
import re
import shutil
import tempfile
import time

from translator import client, localgit
from translator.common import AUTO_BRANCH_PREFIX


def _find_existing_fork(org, repo, username, token):
    """在目标仓库的 Fork 列表中查找当前用户的已有 Fork。

    Fork 后的仓库路径可能与原仓库名不同（如 Ascend/msprof 的 Fork
    可能是 user/MindStudio-Profiler），不能按路径猜测；用户仓库列表
    接口又不保证返回 parent 字段，因此改为列出目标仓库的全部 Fork，
    按 owner 匹配当前用户。

    Returns:
        str | None: 已有 Fork 的 full_name；没有则返回 None。
    """
    page = 1
    while True:
        forks = client.list_repo_forks(org, repo, token, page=page)
        for f in forks:
            owner = f.get("owner") or {}
            if username in (owner.get("login"), owner.get("username")):
                # full_name 缺失时退化为 owner/path 拼接
                full = f.get("full_name")
                if not full and f.get("path"):
                    full = f"{username}/{f['path']}"
                if full:
                    return full
        if len(forks) < 100:
            return None
        page += 1


def _wait_fork_ready(owner, repo_name, token, log, attempts=10, interval=3):
    """Fork 是异步操作，刚创建后仓库可能暂时不可访问，轮询等待其就绪。

    Returns:
        bool: 仓库可访问返回 True，超时仍未就绪返回 False。
    """
    for i in range(attempts):
        try:
            client.get_repo(owner, repo_name, token)
            return True
        except client.AtomGitError as e:
            if e.status_code != 404 and "not found" not in str(e).lower():
                raise
            log(f"Fork 仓库初始化中，等待就绪 ({i + 1}/{attempts})...")
            time.sleep(interval)
    return False


def _sync_fork_branch(owner, repo_name, branch, token, log,
                      attempts=10, interval=2):
    """同步 Fork 仓库的指定分支并轮询等待完成。

    Fork 不会自动跟进上游新增/更新的分支：上游的 commit sha 和新分支名
    在未同步的 Fork 上都不存在，直接建分支会 404（建分支前的必要步骤）。
    同步为异步操作：PUT 触发后轮询 GET 状态直到 repo_sync_result 为真。
    按分支同步要求该分支在 Fork 上已存在（否则 400），此时退化为
    全量同步（不传 branch）——全量同步会把上游新增分支补进 Fork；
    全量同步也是异步的，且状态接口对尚不存在的分支不可靠，故触发后
    轮询分支列表等待目标分支真正出现（超时仍继续，建分支有兜底）。
    同步接口最终失败或轮询超时都不阻断流程——后续建分支仍有
    sha/分支名两级兜底。
    """
    log(f"正在同步 Fork 仓库分支: {branch}")
    try:
        client.sync_repo(owner, repo_name, token, branch=branch)
    except client.AtomGitError as e:
        if e.status_code != 400:
            log(f"Fork 分支同步未完成（{e}），继续尝试建分支")
            return
        # 按分支同步 400 说明 Fork 上没有该分支，退化为全量同步
        log("按分支同步失败（Fork 缺少该分支），尝试全量同步...")
        try:
            client.sync_repo(owner, repo_name, token)
        except client.AtomGitError as e2:
            log(f"Fork 分支同步未完成（{e2}），继续尝试建分支")
            return
        # 全量同步是异步的，且 get_sync_status 对 Fork 上尚不存在的分支
        # 不可靠（可能立即返回成功），改为轮询分支列表等该分支出现
        for i in range(attempts):
            try:
                names = {b.get("name")
                         for b in client.list_branches(owner, repo_name, token)}
            except client.AtomGitError:
                names = set()
            if branch in names:
                log(f"全量同步完成，Fork 已出现分支: {branch}")
                return
            log(f"等待 Fork 全量同步 ({i + 1}/{attempts})...")
            time.sleep(interval)
        log(f"Fork 全量同步超时（未等到分支 {branch}），继续尝试建分支")
        return
    try:
        for i in range(attempts):
            status = client.get_sync_status(owner, repo_name, token,
                                            branch=branch)
            if not isinstance(status, dict) or status.get("repo_sync_result"):
                # 把平台返回的同步结果消息打进日志，便于排查同步问题
                if isinstance(status, dict) and status.get("repo_sync_message"):
                    log(f"Fork 分支同步结果: {status['repo_sync_message']}")
                return
            log(f"等待 Fork 分支同步 ({i + 1}/{attempts})...")
            time.sleep(interval)
    except client.AtomGitError as e:
        log(f"Fork 分支同步未完成（{e}），继续尝试建分支")


def _make_pr_title_body(succeeded):
    """根据已写入文件列表生成 PR 标题和描述。

    单文件时用 "新增/更新 <文件名> 英文版文档" 格式；
    多文件时合并为 "新增 N 份，更新 M 份 英文版文档"。
    描述正文按新增/更新分组列出文件路径。
    """
    created = [f for f in succeeded if f["action"] == "create"]
    updated = [f for f in succeeded if f["action"] == "update"]

    if len(succeeded) == 1:
        action = "新增" if created else "更新"
        filename = os.path.basename(succeeded[0]["path"])
        title = f"{action} {filename} 英文版文档"
    else:
        parts = []
        if created:
            parts.append(f"新增 {len(created)} 份")
        if updated:
            parts.append(f"更新 {len(updated)} 份")
        title = "，".join(parts) + " 英文版文档"

    lines = []
    if created:
        lines.append("## 新增")
        for f in created:
            lines.append(f"- `{f['path']}`")
    if updated:
        if lines:
            lines.append("")
        lines.append("## 更新")
        for f in updated:
            lines.append(f"- `{f['path']}`")

    return title, "\n".join(lines)


def _branch_url(org, repo, branch):
    """目标仓分支链接：内容已在目标分支时的「已合入」标识。

    ``/api/v1/prs/status`` 认得这种链接并直接返回 ``state=merged``，
    让调用方据此把任务推进到「已上线」，而不必发明一个不存在的 PR。
    """
    return f"https://atomgit.com/{org}/{repo}/tree/{branch}"


def _find_previous_pr(org, repo, head, branch, token, log):
    """回捞该 head/base 上的历史 PR（含已合并/已关闭）。

    noop 场景（内容已与目标分支一致、且无未合并 PR）时仍需给出 PR 链接，
    调用方才能据其判定「已上线」；这里按「已合并优先、编号最大优先」取
    最近一条。查询失败一律返回 None，不影响主流程。

    Returns:
        dict | None: {"pr_url", "pr_number"}。
    """
    for state in ("all", "closed"):
        try:
            prs = client.list_pull_requests(org, repo, token, state=state)
        except Exception as exc:
            log(f"回查历史 PR 失败（state={state}）：{exc}")
            continue
        matched = [p for p in prs if _pr_matches(p, head, branch)]
        if not matched:
            continue
        matched.sort(
            key=lambda p: (bool(p.get("merged") or p.get("merged_at")),
                           p.get("number") or 0),
            reverse=True)
        best = matched[0]
        number = best.get("number")
        url = (best.get("html_url")
               or f"https://atomgit.com/{org}/{repo}/merge_requests/{number}")
        return {"pr_url": url, "pr_number": number}
    return None


def _pr_matches(pr, head, branch):
    """判断 PR 是否匹配指定的 head（用户:分支）与 base 分支。

    API 的 head/base 过滤参数可能不被服务端支持，客户端再校验一次。
    """
    head_info = pr.get("head") or {}
    base_info = pr.get("base") or {}
    head_ok = (head_info.get("label") == head
               or head_info.get("ref") == head.split(":", 1)[-1])
    base_ref = base_info.get("ref") or base_info.get("label", "").split(":")[-1]
    return head_ok and (not base_ref or base_ref == branch)


def submit_repo(org, repo, branch, files, token, log=print):
    """把一个仓库的变更译稿幂等地提交为目标社区仓库的 Pull Request。

    执行阶段：识别用户/Fork → 校验目标分支 → 同步 Fork 分支 →
    以目标分支为基点重建固定分支（全部变更合并为单个 commit，
    由 localgit.rebuild_branch 本地批量写入并强推）→ 去重后创建 PR。

    Returns:
        dict with ok, pr_url, pr_number, pr_existed, head_branch,
        succeeded / stage, error
    """
    if not files:
        return {"ok": False, "stage": "validate", "error": "文件清单为空"}

    # ── 阶段 0：识别用户并 Fork 目标仓库 ──
    log("正在识别用户并 Fork 目标仓库...")
    try:
        user_info = client.get_user(token)
        fork_owner = user_info.get("login") or user_info.get("username")
        if not fork_owner:
            return {"ok": False, "stage": "get_user", "error": "无法从 token 获取用户名"}
        # 账号绑定邮箱：commit 作者邮箱需与 CLA 签署邮箱一致
        author_email = user_info.get("email") or None
    except client.AtomGitError as e:
        return {"ok": False, "stage": "get_user", "error": str(e)}

    log(f"当前用户: {fork_owner}")

    # Fork 后的仓库路径可能与原仓库名不同，先按 Fork 列表查找已有 Fork
    try:
        fork_full = _find_existing_fork(org, repo, fork_owner, token)
    except client.AtomGitError as e:
        return {"ok": False, "stage": "fork_repo", "error": str(e)}

    fork_ready = fork_full is not None
    if fork_full:
        log(f"使用已有 Fork: {fork_full}")
    else:
        try:
            fork = client.fork_repo(org, repo, token)
            fork_full = fork.get("full_name") or ""
        except client.AtomGitError as e:
            msg = str(e)
            if "already exists" not in msg and "已存在" not in msg and "已经" not in msg:
                return {"ok": False, "stage": "fork_repo", "error": msg}
            fork_full = ""
        if not fork_full:
            # 未返回仓库名或提示已存在：Fork 是异步操作，
            # 轮询 Fork 列表等待其出现
            for attempt in range(5):
                try:
                    fork_full = _find_existing_fork(org, repo, fork_owner, token)
                except client.AtomGitError as e:
                    return {"ok": False, "stage": "fork_repo", "error": str(e)}
                if fork_full:
                    break
                log(f"等待 Fork 仓库创建 ({attempt + 1}/5)...")
                time.sleep(3)
        if not fork_full:
            return {
                "ok": False,
                "stage": "fork_repo",
                "error": f"Fork {org}/{repo} 失败，无法确定 Fork 仓库",
            }
        log(f"Fork 仓库: {fork_full}")

    # Fork 是异步操作，新建的 Fork 需等待就绪后再写入；
    # 后续操作使用 Fork 实际返回的仓库名
    fork_repo_name = fork_full.split("/")[-1]
    if not fork_ready:
        try:
            ready = _wait_fork_ready(fork_owner, fork_repo_name, token, log)
        except client.AtomGitError as e:
            return {"ok": False, "stage": "fork_repo", "error": str(e)}
        if not ready:
            return {
                "ok": False,
                "stage": "fork_repo",
                "error": f"Fork 仓库 {fork_full} 初始化超时，请稍后重试",
            }

    # ── 阶段 1：确定并校验目标分支 ──
    if not branch:
        # 未指定分支时，使用目标仓库默认分支
        try:
            target_info = client.get_repo(org, repo, token)
            branch = target_info.get("default_branch") or "master"
        except client.AtomGitError as e:
            return {"ok": False, "stage": "check_branch", "error": str(e)}
        log(f"未指定目标分支，使用目标仓库默认分支: {branch}")

    log(f"正在校验目标分支: {branch}")
    try:
        branches = client.list_branches(org, repo, token)
    except client.AtomGitError as e:
        return {"ok": False, "stage": "check_branch", "error": str(e)}
    sha_by_name = {b.get("name"): (b.get("commit") or {}).get("sha")
                   for b in branches}
    if branch not in sha_by_name:
        return {
            "ok": False,
            "stage": "check_branch",
            "error": f"目标仓库 {org}/{repo} 不存在分支 {branch}，请确认版本号",
        }

    # ── 阶段 2：同步 Fork 仓库的目标分支 ──
    # Fork 不会自动跟进上游新增/更新的分支，重建以 Fork 上的目标分支为
    # 基点，克隆前先同步保证基点与上游一致
    _sync_fork_branch(fork_owner, fork_repo_name, branch, token, log)

    # ── 阶段 3：以目标分支为基点重建固定分支（全部变更合并为单个 commit）──
    head_branch = f"{AUTO_BRANCH_PREFIX}{branch}"
    dest_dir = os.path.join(
        tempfile.gettempdir(),
        f"translator-{org}-{repo}-{head_branch}".replace(os.sep, "-"))
    log(f"正在写入 {len(files)} 个文件（合并为单个 commit）...")
    try:
        result = localgit.rebuild_branch(
            fork_owner, fork_repo_name, token, branch, head_branch,
            files, dest_dir, log, author_email=author_email)
    except client.AtomGitError as e:
        return {"ok": False, "stage": "write", "error": str(e),
                "head_branch": head_branch}
    except Exception as e:
        return {"ok": False, "stage": "write", "error": f"写入失败: {e}",
                "head_branch": head_branch}
    finally:
        shutil.rmtree(dest_dir, ignore_errors=True)

    succeeded = result["written"]
    if not result["pushed"]:
        log(f"分支内容已是最新，无需推送（{result['reason']}）")

    # ── 阶段 5：PR 去重——已存在同一 head/base 的 open PR 时不重复创建 ──
    head = f"{fork_owner}:{head_branch}"
    log("正在检查是否已有未合并的 PR...")
    try:
        open_prs = client.list_pull_requests(org, repo, token, state="open")
    except client.AtomGitError as e:
        return {"ok": False, "stage": "list_pr", "error": str(e)}
    existing_pr = next(
        (p for p in open_prs if _pr_matches(p, head, branch)), None)
    if existing_pr:
        number = existing_pr.get("number")
        pr_url = (existing_pr.get("html_url")
                  or f"https://atomgit.com/{org}/{repo}/merge_requests/{number}")
        log(f"已存在未合并的 PR，本次变更已并入: {pr_url}")
        return {
            "ok": True,
            "pr_existed": True,
            "pr_url": pr_url,
            "pr_number": number,
            "head_branch": head_branch,
            "succeeded": succeeded,
        }

    # 无已开 PR 且本次无新变更（或仅压缩了存量历史）：没有可创建的 PR
    if not succeeded:
        previous = _find_previous_pr(org, repo, head, branch, token, log)
        if previous:
            log(f"无新变更且无未合并 PR，回传历史 PR: {previous['pr_url']}")
            return {
                "ok": True,
                "pr_existed": True,
                "pr_url": previous["pr_url"],
                "pr_number": previous["pr_number"],
                "head_branch": head_branch,
                "succeeded": succeeded,
            }
        # 连历史 PR 都没有：说明内容已在目标分支（Fork 分支已与上游同步），
        # 业务上等价于「已合入」。返回目标分支链接，由 /api/v1/prs/status
        # 识别为 merged，避免调用方把任务永久停在「已提交PR」。
        log("无新变更且无历史 PR：内容已在目标分支，回传目标分支链接")
        return {
            "ok": True,
            "pr_existed": True,
            "content_exists": True,
            "pr_url": _branch_url(org, repo, branch),
            "pr_number": None,
            "head_branch": head_branch,
            "succeeded": succeeded,
        }

    # ── 阶段 6：创建 Pull Request ──
    pr_title, pr_body = _make_pr_title_body(succeeded)
    marker = "由 translator 服务自动提交"
    pr_body = f"{pr_body}\n\n---\n{marker}" if pr_body else marker

    log("正在创建 PR...")
    try:
        pr = client.create_pr(
            org, repo, pr_title, head, branch, pr_body,
            token, fork_path=fork_full,
        )
    except client.AtomGitError as e:
        # 兜底：去重查询漏判（或并发）时服务端返回 409 并带出已有 PR 号，
        # 按「已存在未合并 PR」处理而非失败
        if e.status_code == 409:
            m = re.search(r"!(\d+)", e.message or "")
            number = int(m.group(1)) if m else None
            pr_url = (f"https://atomgit.com/{org}/{repo}/merge_requests/{number}"
                      if number else None)
            log(f"服务端提示已存在未合并的 PR，本次变更已并入: {pr_url or e.message}")
            return {
                "ok": True,
                "pr_existed": True,
                "pr_url": pr_url,
                "pr_number": number,
                "head_branch": head_branch,
                "succeeded": succeeded,
            }
        return {
            "ok": False,
            "stage": "create_pr",
            "error": str(e),
            "head_branch": head_branch,
            "succeeded": succeeded,
        }

    pr_number = pr.get("number")
    pr_url = (pr.get("html_url")
              or f"https://atomgit.com/{org}/{repo}/merge_requests/{pr_number}")
    return {
        "ok": True,
        "pr_existed": False,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "head_branch": head_branch,
        "succeeded": succeeded,
    }
