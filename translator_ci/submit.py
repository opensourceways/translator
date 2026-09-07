"""幂等提交：Fork 目标仓库、维护固定分支、写入文件并创建/复用 PR。

submit_repo: 单仓库幂等提交（识别用户/Fork → 校验目标分支 → 同步
             Fork 分支 → 复用或创建固定分支 translator/auto-<目标分支>
             → 清理陈旧文件 → 写入文件 → 去重后创建 PR）
submit_all:  批量提交（Token 路由 + 单仓失败不中断）

幂等语义：同一仓库重复运行时，已开 PR 不重复创建，新变更以新 commit
进入同一 PR；分支内容已是最新时不产生空 commit。源目录中已消失的
文件在复用分支时自动删除，避免 PR 合入陈旧残留。
"""

import base64
import os
import re
import time

from translator_ci import client
from translator_ci.common import AUTO_BRANCH_PREFIX, _git_blob_sha
from translator_ci.rules import resolve_token, _effective_rules


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
        except client.GitCodeError as e:
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
    except client.GitCodeError as e:
        if e.status_code != 400:
            log(f"Fork 分支同步未完成（{e}），继续尝试建分支")
            return
        # 按分支同步 400 说明 Fork 上没有该分支，退化为全量同步
        log("按分支同步失败（Fork 缺少该分支），尝试全量同步...")
        try:
            client.sync_repo(owner, repo_name, token)
        except client.GitCodeError as e2:
            log(f"Fork 分支同步未完成（{e2}），继续尝试建分支")
            return
        # 全量同步是异步的，且 get_sync_status 对 Fork 上尚不存在的分支
        # 不可靠（可能立即返回成功），改为轮询分支列表等该分支出现
        for i in range(attempts):
            try:
                names = {b.get("name")
                         for b in client.list_branches(owner, repo_name, token)}
            except client.GitCodeError:
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
    except client.GitCodeError as e:
        log(f"Fork 分支同步未完成（{e}），继续尝试建分支")


def _branch_404_hint(error, fork_full, branch):
    """为建分支失败生成错误文案：404 时补充 Fork 未同步的排查提示。"""
    msg = str(error)
    if getattr(error, "status_code", None) == 404:
        msg += (f"（Fork 仓库 {fork_full} 上不存在分支 {branch} 的引用，"
                f"同步可能未完成或上游该分支为新增；"
                f"可在 GitCode 网页打开 Fork 仓库手动同步后重试）")
    return msg


def _make_pr_title_body(succeeded):
    """根据已写入文件列表生成 PR 标题和描述。

    单文件时用 "新增/更新/移除 <文件名> 英文版文档" 格式；
    多文件时合并为 "新增 N 份，更新 M 份，移除 K 份 英文版文档"。
    描述正文按新增/更新/移除分组列出文件路径。
    """
    created = [f for f in succeeded if f["action"] == "create"]
    updated = [f for f in succeeded if f["action"] == "update"]
    removed = [f for f in succeeded if f["action"] == "delete"]

    if len(succeeded) == 1:
        if removed:
            filename = os.path.basename(removed[0]["path"])
            title = f"移除 {filename} 英文版文档"
        else:
            action = "新增" if created else "更新"
            filename = os.path.basename(succeeded[0]["path"])
            title = f"{action} {filename} 英文版文档"
    else:
        parts = []
        if created:
            parts.append(f"新增 {len(created)} 份")
        if updated:
            parts.append(f"更新 {len(updated)} 份")
        if removed:
            parts.append(f"移除 {len(removed)} 份")
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
    if removed:
        if lines:
            lines.append("")
        lines.append("## 移除（源目录已不存在）")
        for f in removed:
            lines.append(f"- `{f['path']}`")

    return title, "\n".join(lines)


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


def _find_open_pr(org, repo, fork_owner, head_branch, branch, token):
    """查找 fork 固定分支指向目标分支的已开 PR。

    Returns:
        dict | None: 匹配的 open PR；查不到或查询失败返回 None。
    """
    head = f"{fork_owner}:{head_branch}"
    try:
        open_prs = client.list_pull_requests(org, repo, token, state="open")
    except client.GitCodeError:
        return None
    return next((p for p in open_prs if _pr_matches(p, head, branch)), None)


def submit_repo(org, repo, branch, files, token, log=print, all_paths=None):
    """把一个仓库的变更译稿幂等地提交为目标社区仓库的 Pull Request。

    执行阶段：识别用户/Fork → 校验目标分支 → 同步 Fork 分支 →
    复用或创建固定分支（translator/auto-<目标分支>）→ 清理陈旧文件
    （复用分支且提供 all_paths 时）→ 写入文件（与分支现有内容相同的
    跳过）→ 去重后创建 PR。

    陈旧文件清理：源目录结构调整（文件改名/搬家）后，固定分支上会
    残留已不在源目录中的旧文件，随 PR 合入会污染目标仓库。清理规则
    为「该分支已开 PR 的 added 文件 − 当前源文件全集（all_paths）」
    ——added 即「分支上有、上游没有」，一次 PR diff 请求即得差集。
    查询失败或无已开 PR 时跳过清理（宁可不删，不可误删）。

    Args:
        all_paths: 该仓库全部译稿的目标路径列表（scan 阶段产出）。
                   不传（None）时不做清理，保持向后兼容。

    Returns:
        dict with ok, pr_url, pr_number, pr_existed, head_branch,
        succeeded, unchanged, deleted, failed / stage, error
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
    except client.GitCodeError as e:
        return {"ok": False, "stage": "get_user", "error": str(e)}

    log(f"当前用户: {fork_owner}")

    # Fork 后的仓库路径可能与原仓库名不同，先按 Fork 列表查找已有 Fork
    try:
        fork_full = _find_existing_fork(org, repo, fork_owner, token)
    except client.GitCodeError as e:
        return {"ok": False, "stage": "fork_repo", "error": str(e)}

    fork_ready = fork_full is not None
    if fork_full:
        log(f"使用已有 Fork: {fork_full}")
    else:
        try:
            fork = client.fork_repo(org, repo, token)
            fork_full = fork.get("full_name") or ""
        except client.GitCodeError as e:
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
                except client.GitCodeError as e:
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
        except client.GitCodeError as e:
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
        except client.GitCodeError as e:
            return {"ok": False, "stage": "check_branch", "error": str(e)}
        log(f"未指定目标分支，使用目标仓库默认分支: {branch}")

    log(f"正在校验目标分支: {branch}")
    try:
        branches = client.list_branches(org, repo, token)
    except client.GitCodeError as e:
        return {"ok": False, "stage": "check_branch", "error": str(e)}
    sha_by_name = {b.get("name"): (b.get("commit") or {}).get("sha")
                   for b in branches}
    if branch not in sha_by_name:
        return {
            "ok": False,
            "stage": "check_branch",
            "error": f"目标仓库 {org}/{repo} 不存在分支 {branch}，请确认版本号",
        }
    # 记录上游分支的 commit sha：建分支时优先用 sha 作为 refs，
    # 避免 Fork 上的同名分支落后于上游导致 PR 基准过时
    base_sha = sha_by_name[branch]

    # ── 阶段 2：同步 Fork 仓库的目标分支 ──
    # Fork 不会自动跟进上游新增/更新的分支（上游的 commit sha 和新分支名
    # 在未同步的 Fork 上都不存在，直接建分支会 404），建分支前先同步
    _sync_fork_branch(fork_owner, fork_repo_name, branch, token, log)

    # ── 阶段 3：复用或创建固定分支 ──
    head_branch = f"{AUTO_BRANCH_PREFIX}{branch}"
    try:
        fork_branches = client.list_branches(fork_owner, fork_repo_name, token)
        fork_branch_names = {b.get("name") for b in fork_branches}
    except client.GitCodeError as e:
        return {"ok": False, "stage": "create_branch", "error": str(e)}

    if head_branch in fork_branch_names:
        log(f"复用已有分支: {head_branch}")
    else:
        log(f"正在创建分支: {head_branch}")
        try:
            client.create_branch(
                fork_owner, fork_repo_name, head_branch, base_sha or branch, token)
        except client.GitCodeError as e:
            # 同步后 sha 在 Fork 上仍不可用时，退化为分支名再试一次
            if not (base_sha and e.status_code == 404):
                return {"ok": False, "stage": "create_branch",
                        "error": _branch_404_hint(e, fork_full, branch)}
            try:
                client.create_branch(
                    fork_owner, fork_repo_name, head_branch, branch, token)
            except client.GitCodeError as e2:
                return {"ok": False, "stage": "create_branch",
                        "error": _branch_404_hint(e2, fork_full, branch)}

    # ── 阶段 3.5：清理陈旧文件（仅复用已有分支且提供全量清单时）──
    # 新建分支等于上游快照，不可能有残留；只处理复用分支的场景。
    # 陈旧候选 = 该分支已开 PR 的 added 文件 − 当前源文件全集：added 即
    # 「分支上有、上游没有」，一次 PR diff 请求即可得差集，免去逐目录
    # 遍历双仓库的上百次请求（#61 实测限流下单仓清点 20 分钟）。
    succeeded = []
    unchanged = []
    deleted = []
    failed = []
    if head_branch in fork_branch_names and all_paths is not None:
        existing_pr = _find_open_pr(
            org, repo, fork_owner, head_branch, branch, token)
        if existing_pr is None:
            log("未找到已开的 PR，跳过陈旧文件清理（宁可不删，不可误删）")
            stale = []
        else:
            try:
                pr_files = client.list_pr_files(
                    org, repo, existing_pr["number"], token)
                added = {f["filename"] for f in pr_files
                         if f.get("status") == "added"}
                stale = sorted(added - set(all_paths))
            except client.GitCodeError as e:
                log(f"陈旧文件检查失败（{e}），跳过清理")
                stale = []
        for i, path in enumerate(stale, 1):
            log(f"正在清理陈旧文件 ({i}/{len(stale)}): {path}")
            try:
                existing = client.get_contents(
                    fork_owner, fork_repo_name, path, head_branch, token)
                sha = existing.get("sha") if isinstance(existing, dict) else None
                if not sha:
                    raise client.GitCodeError(404, "文件不存在或未返回 sha")
                client.delete_file(
                    fork_owner, fork_repo_name, path,
                    f"移除 {os.path.basename(path)}（源目录已不存在）",
                    sha, head_branch, token)
                deleted.append(path)
            except Exception as e:
                failed.append({"file": path, "error": f"清理失败: {e}"})

    # ── 阶段 4：写入文件（与分支现有内容相同的跳过，避免空 commit）──
    for i, f in enumerate(files, 1):
        log(f"正在写入文件 ({i}/{len(files)}): {f['target_path']}")
        filename = os.path.basename(f["target_path"])
        try:
            existing = client.get_contents(
                fork_owner, fork_repo_name, f["target_path"], head_branch, token
            )
            if existing is None:
                msg = f"新增 {filename} 英文版文档"
                client.create_file(
                    fork_owner, fork_repo_name, f["target_path"], msg,
                    f["content_b64"], head_branch, token,
                )
                succeeded.append({"path": f["target_path"], "action": "create"})
            elif isinstance(existing, list):
                # contents API 对目录返回数组：目标路径已存在同名目录，无法写入
                failed.append({
                    "file": f["target_path"],
                    "error": "目标仓库已存在同名目录，无法写入同名文件",
                })
            else:
                sha = existing.get("sha")
                if not sha:
                    failed.append({
                        "file": f["target_path"], "error": "API returned no sha",
                    })
                    continue
                content = base64.b64decode(f["content_b64"])
                if sha == _git_blob_sha(content):
                    unchanged.append(f["target_path"])
                    log(f"  内容相同，跳过: {f['target_path']}")
                    continue
                msg = f"更新 {filename} 英文版文档"
                client.update_file(
                    fork_owner, fork_repo_name, f["target_path"], msg,
                    f["content_b64"], sha, head_branch, token,
                )
                succeeded.append({"path": f["target_path"], "action": "update"})
        except Exception as e:
            failed.append({"file": f["target_path"], "error": str(e)})

    if not succeeded and not unchanged and not deleted:
        # 没有任何文件写入/清理成功且分支上也没有可用内容：不能创建空 PR
        return {
            "ok": False,
            "stage": "write",
            "error": "所有文件均写入失败，未创建 PR",
            "head_branch": head_branch,
            "succeeded": succeeded,
            "deleted": deleted,
            "failed": failed,
        }
    if not succeeded:
        log("分支内容已是最新，无需写入")

    # ── 阶段 5：PR 去重——已存在同一 head/base 的 open PR 时不重复创建 ──
    head = f"{fork_owner}:{head_branch}"
    log("正在检查是否已有未合并的 PR...")
    try:
        open_prs = client.list_pull_requests(org, repo, token, state="open")
    except client.GitCodeError as e:
        return {"ok": False, "stage": "list_pr", "error": str(e)}
    existing_pr = next(
        (p for p in open_prs if _pr_matches(p, head, branch)), None)
    if existing_pr:
        number = existing_pr.get("number")
        pr_url = (existing_pr.get("html_url")
                  or f"https://gitcode.com/{org}/{repo}/merge_requests/{number}")
        log(f"已存在未合并的 PR，本次变更已并入: {pr_url}")
        return {
            "ok": True,
            "pr_existed": True,
            "pr_url": pr_url,
            "pr_number": number,
            "head_branch": head_branch,
            "succeeded": succeeded,
            "unchanged": unchanged,
            "deleted": deleted,
            "failed": failed,
        }

    # ── 阶段 6：创建 Pull Request ──
    pr_files = succeeded or [
        {"path": p, "action": a} for p, a in
        [(u, "update") for u in unchanged] + [(d, "delete") for d in deleted]]
    pr_title, pr_body = _make_pr_title_body(pr_files)
    marker = "由 Jenkins 自动提交（translator-ci）"
    pr_body = f"{pr_body}\n\n---\n{marker}" if pr_body else marker

    log("正在创建 PR...")
    try:
        pr = client.create_pr(
            org, repo, pr_title, head, branch, pr_body,
            token, fork_path=fork_full,
        )
    except client.GitCodeError as e:
        # 兜底：去重查询漏判（或并发）时服务端返回 409 并带出已有 PR 号，
        # 按「已存在未合并 PR」处理而非失败
        if e.status_code == 409:
            m = re.search(r"!(\d+)", e.message or "")
            number = int(m.group(1)) if m else None
            pr_url = (f"https://gitcode.com/{org}/{repo}/merge_requests/{number}"
                      if number else None)
            log(f"服务端提示已存在未合并的 PR，本次变更已并入: {pr_url or e.message}")
            return {
                "ok": True,
                "pr_existed": True,
                "pr_url": pr_url,
                "pr_number": number,
                "head_branch": head_branch,
                "succeeded": succeeded,
                "unchanged": unchanged,
                "deleted": deleted,
                "failed": failed,
            }
        return {
            "ok": False,
            "stage": "create_pr",
            "error": str(e),
            "head_branch": head_branch,
            "succeeded": succeeded,
            "unchanged": unchanged,
            "deleted": deleted,
            "failed": failed,
        }

    pr_number = pr.get("number")
    pr_url = (pr.get("html_url")
              or f"https://gitcode.com/{org}/{repo}/merge_requests/{pr_number}")
    return {
        "ok": True,
        "pr_existed": False,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "head_branch": head_branch,
        "succeeded": succeeded,
        "unchanged": unchanged,
        "deleted": deleted,
        "failed": failed,
    }


def submit_all(changed, token, token_rules=None, log=print):
    """对有变更的仓库逐个幂等提交 PR，单仓失败不中断。

    每个仓库使用的 Token 由 resolve_token 按 token_rules 路由
    （权限管控）：配置了有效规则时，未命中规则的仓库记为失败
    不予提交；未配置规则时全部使用默认 token。

    Args:
        changed: scan_changed 返回的 changed 清单，
                 每项含 org/repo/branch/files/all_paths/project
        token: 默认 GitCode Token（未命中规则时使用）
        token_rules: Token 规则 [{keyword, token}]，按路径关键字匹配
        log: 日志函数

    Returns:
        dict with:
        - ok: bool
        - results: [{path, ok, pr_url?, pr_number?, pr_existed?,
                     stage?, error?}]
        - submitted: 成功提 PR 的仓库数
        - failed: 失败的仓库数
    """
    token = (token or "").strip()
    if not token:
        return {"ok": False, "stage": "validate", "error": "默认 Token 不能为空"}

    changed = changed or []
    results = []
    submitted = 0
    failed_count = 0
    # 配置了有效规则时只提交命中规则的仓库（权限管控，与扫描一致）
    restrict = bool(_effective_rules(token_rules))
    for i, item in enumerate(changed, 1):
        path = item["project"]["path"]
        log(f"正在提交 ({i}/{len(changed)}): {item['org']}/{item['repo']}")
        item_token, rule_keyword = resolve_token(path, token_rules, token)
        if restrict and not rule_keyword:
            failed_count += 1
            results.append({
                "path": path, "ok": False, "stage": "token_rule",
                "error": "未命中 Token 规则，按权限管控不处理",
            })
            continue
        if rule_keyword:
            log(f"  命中 Token 规则「{rule_keyword}」，使用规则 Token 提交")
        result = submit_repo(
            item["org"], item["repo"], item["branch"], item["files"],
            item_token, log, all_paths=item.get("all_paths"))
        entry = {"path": path, "ok": result["ok"]}
        if result["ok"]:
            submitted += 1
            entry["pr_url"] = result.get("pr_url")
            entry["pr_number"] = result.get("pr_number")
            if result.get("pr_existed"):
                entry["pr_existed"] = True
        else:
            failed_count += 1
            entry["stage"] = result.get("stage")
            entry["error"] = result.get("error")
        results.append(entry)

    return {
        "ok": True,
        "results": results,
        "submitted": submitted,
        "failed": failed_count,
    }
