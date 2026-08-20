"""译稿自动提 PR 的核心业务编排层。

译稿权威来源为 GitCode 私有仓库
Computing_Ecosystem_Docs/Computing_Ecosystem_Docs-EN。
仓库内目录层级固定为「组织/版本/产品/仓库/目标路径」，例如
Ascend/26.1.0/MindStudio/msprof/docs/en/guide.md：
  第 1 段    目标社区组织名（org）
  第 2 段    目标分支名（版本号）
  第 3 段    产品名（仅作分组展示）
  第 4 段    目标仓库名（repo）
  第 5 段起  译稿文件在目标仓库中的真实存放路径

为 Jenkins 无人值守场景设计：
- 无状态：每次运行全量扫描对比（git blob sha 逐文件比对），不依赖任何
  本地缓存；两次触发之间的任意多个 commit 自动汇总为一个 PR
- 幂等提交：固定分支 translator/auto-<目标分支>、与分支现有内容相同的
  文件跳过写入、目标仓库已存在同一 head/base 的 open PR 时不重复创建
  （本次新写入的 commit 自动进入已开 PR）
- Token 规则（权限管控）：规则文件存于译稿权威仓库（默认根目录
  token_rules.json），每次运行时读取；文件不存在视为未配置规则（全部
  使用默认 Token）；配置了有效规则时只处理命中规则的仓库
- 来源可替换：默认读取权威仓库，可用 GITCODE_SOURCE_REPO 环境变量
  指向自己的 Fork（无权威仓库管理权限时测试用，Webhook 配在 Fork 上）；
  该模式下可用 sync_source 在扫描前先同步 Fork 与上游

流程函数：
  load_token_rules: 读取并解析规则文件（JSON 非法抛 ConfigError）
  scan_changed:     扫描全部仓库级目录 → 与目标仓库对比 → 有变更清单
  submit_all:       对有变更仓库逐个幂等提交 PR，单仓失败不中断

任意阶段失败立即返回错误信息，已写入的文件不会回滚。
"""

import base64
import hashlib
import json
import os
import re
import time

from translator_ci import client

# 译稿权威来源仓库（默认）；可用 GITCODE_SOURCE_REPO 环境变量覆盖为
# 自己的 Fork（如 "lei0308/Computing_Ecosystem_Docs-EN"），便于在
# 没有权威仓库管理权限时测试（Webhook 配在 Fork 上）
SOURCE_ORG = "Computing_Ecosystem_Docs"
SOURCE_REPO = "Computing_Ecosystem_Docs-EN"


def _source_repo():
    """返回译稿来源仓库 (owner, repo)，优先取 GITCODE_SOURCE_REPO 环境变量。

    对环境变量的写法做宽容解析：允许粘贴完整 URL
    （https://gitcode.com/owner/repo）或带 .git 后缀，只取最后两段。
    """
    full = os.environ.get("GITCODE_SOURCE_REPO", "").strip()
    if full:
        full = re.sub(r"^https?://[^/]+/", "", full)
        full = full.removesuffix(".git").strip("/")
        parts = full.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return SOURCE_ORG, SOURCE_REPO

DEFAULT_RULES_PATH = "token_rules.json"  # 权威仓库根目录下的规则文件
MAX_TOTAL_SIZE = 5 * 1024 * 1024         # 单次上传累计文件大小上限 5 MB
MAX_DEPTH = 6                            # 目录索引的最大递归深度
AUTO_BRANCH_PREFIX = "translator/auto-"  # 自动提交的固定分支名前缀


class ConfigError(Exception):
    """规则配置损坏（JSON 非法等）时抛出——权限边界配置不能静默放行。"""

    pass


# ── Token 规则 ────────────────────────────────────────────────────────


def resolve_token(path, token_rules, default_token):
    """按 Token 规则为译稿目录选择访问目标仓库的 Token。

    规则关键字对目录完整路径（如 Ascend/26.1.0/MindStudio/msprof）
    做大小写不敏感的包含匹配，命中第一条规则即返回其 Token；
    关键字或 Token 留空的规则视为无效并跳过；全部未命中时返回
    默认 Token。

    Returns:
        tuple: (token, matched_keyword)，未命中规则时 keyword 为 None
    """
    for rule in token_rules or []:
        if not isinstance(rule, dict):
            continue
        keyword = (rule.get("keyword") or "").strip()
        token = (rule.get("token") or "").strip()
        if keyword and token and keyword.lower() in path.lower():
            return token, keyword
    return default_token, None


def _effective_rules(token_rules):
    """筛出有效 Token 规则（关键字与 Token 均非空的字典条目）。"""
    return [
        r for r in token_rules or []
        if isinstance(r, dict)
        and (r.get("keyword") or "").strip()
        and (r.get("token") or "").strip()
    ]


def load_token_rules(token, rules_path=DEFAULT_RULES_PATH, log=print):
    """从译稿权威仓库读取 Token 规则文件并解析为 [{"keyword", "token"}]。

    规则文件只保存 keyword → 环境变量名的映射，Token 本体不落任何仓库：
    每条规则的 token_env 在运行时解析为环境变量（Jenkins 凭据注入）的值，
    环境变量缺失的规则打印警告并跳过。

    文件不存在（404）时返回空规则——全部仓库使用默认 Token；
    文件存在但 JSON 非法时抛出 ConfigError——权限边界配置损坏不能
    静默放行。

    Returns:
        list: [{"keyword": ..., "token": ...}]，无有效规则时为空列表
    """
    owner, repo = _source_repo()
    log(f"正在从来源仓库 {owner}/{repo} 读取规则文件: {rules_path}")
    repo_info = client.get_repo(owner, repo, token)
    branch = repo_info.get("default_branch") or "master"
    try:
        text = client.get_file_text(
            owner, repo, rules_path, branch, token)
    except client.GitCodeError as e:
        if e.status_code == 404:
            log(f"来源仓库中不存在规则文件 {rules_path}，全部仓库使用默认 Token")
            return []
        raise
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"规则文件 {rules_path} JSON 解析失败: {e}")
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        raise ConfigError(f"规则文件 {rules_path} 格式非法：缺少 rules 列表")

    resolved = []
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            log(f"  [警告] 第 {i} 条规则不是对象，已跳过")
            continue
        keyword = (rule.get("keyword") or "").strip()
        env_name = (rule.get("token_env") or "").strip()
        if not keyword or not env_name:
            log(f"  [警告] 第 {i} 条规则缺少 keyword 或 token_env，已跳过")
            continue
        value = os.environ.get(env_name, "").strip()
        if not value:
            log(f"  [警告] 规则「{keyword}」对应的环境变量 {env_name} 未配置，已跳过")
            continue
        resolved.append({"keyword": keyword, "token": value})

    if resolved:
        log(f"已加载 {len(resolved)} 条 Token 规则（仅处理命中规则的仓库）")
    else:
        log("无有效 Token 规则，全部仓库使用默认 Token")
    return resolved


# ── 来源仓库同步（Fork 模式）─────────────────────────────────────────


def sync_source(token, log=print, attempts=10, interval=2):
    """扫描前同步来源仓库与其上游（来源为 Fork 时使用）。

    没有权威仓库管理权限时，来源可指向自己的 Fork
    （GITCODE_SOURCE_REPO 环境变量），但 Fork 不会自动跟进上游更新，
    扫描前需先同步，否则对比的是旧内容。同步为异步操作，触发后
    轮询等待完成；失败或超时不阻断——扫描仍按 Fork 当前内容继续。
    """
    owner, repo = _source_repo()
    if (owner, repo) == (SOURCE_ORG, SOURCE_REPO):
        log("来源为权威仓库本身，无需同步")
        return
    log(f"正在同步来源 Fork: {owner}/{repo}")
    try:
        client.sync_repo(owner, repo, token)
    except client.GitCodeError as e:
        log(f"来源 Fork 同步触发失败（{e}），按当前内容继续扫描")
        return
    try:
        for i in range(attempts):
            status = client.get_sync_status(owner, repo, token)
            if not isinstance(status, dict) or status.get("repo_sync_result"):
                if isinstance(status, dict) and status.get("repo_sync_message"):
                    log(f"来源 Fork 同步结果: {status['repo_sync_message']}")
                return
            log(f"等待来源 Fork 同步 ({i + 1}/{attempts})...")
            time.sleep(interval)
    except client.GitCodeError as e:
        log(f"来源 Fork 同步未完成（{e}），按当前内容继续扫描")


# ── 目录索引与文件下载 ────────────────────────────────────────────────


def _list_subdirs(path, ref, token):
    """列出权威仓库指定目录下的子目录名（过滤掉文件）。"""
    entries = client.list_dir(*_source_repo(), path, ref, token)
    return [e["name"] for e in entries if e.get("type") == "dir"]


def _walk_files(path, ref, token, prefix=""):
    """递归列出权威仓库指定目录下的全部文件，返回相对路径列表。"""
    files = []
    entries = client.list_dir(*_source_repo(), path, ref, token)
    for e in entries:
        rel = f"{prefix}{e['name']}"
        if e.get("type") == "dir":
            files.extend(_walk_files(e["path"], ref, token, prefix=rel + "/"))
        else:
            files.append(rel)
    return files


def _collect_dirs(path, ref, token, depth, out):
    """递归收集目录树中所有（子树内）包含文件的目录路径。

    Returns:
        int: 该目录子树内的文件总数（用于判断目录是否有效）。
    """
    entries = client.list_dir(*_source_repo(), path, ref, token)
    count = 0
    for e in entries:
        if e.get("type") == "dir":
            if depth < MAX_DEPTH:
                count += _collect_dirs(e["path"], ref, token, depth + 1, out)
        else:
            count += 1
    if path and count:
        out.append(path)
    return count


def _to_project(path):
    """把目录路径按固定层级解析为项目描述。

    目录规范为「组织/版本/产品/仓库/目标路径」：org/version/product
    /project 依次取前四段（不足则为空字符串）；is_repo 标记该目录
    是否为完整的四级（仓库级）目录——只有仓库级目录参与扫描提交。
    """
    segs = path.split("/")
    return {
        "org": segs[0],
        "version": segs[1] if len(segs) > 1 else "",
        "product": segs[2] if len(segs) > 2 else "",
        "project": segs[3] if len(segs) > 3 else "",
        "path": path,
        "is_repo": len(segs) == 4,
    }


def _git_blob_sha(content_bytes):
    """按 git 规则计算文件内容的 blob sha。

    git 对 blob 对象的 sha 计算方式为 sha1("blob <字节数>\\0" + 内容)，
    与 contents API 返回的文件 sha 一致，可直接比对判断目标仓库
    文件与译稿内容是否相同，避免下载目标文件全文。
    """
    header = f"blob {len(content_bytes)}".encode("ascii") + b"\0"
    return hashlib.sha1(header + content_bytes).hexdigest()


def _fetch_repo_files(project, branch, token, log):
    """列出并下载仓库级目录的全部译稿文件。

    目标路径为仓库级目录内的相对路径（即目标仓库中的真实存放路径），
    做路径穿越校验；单次累计大小上限 5 MB。

    Returns:
        dict: {"ok": bool, "files": [{target_path, content_b64}], "error": ...}
    """
    log(f"正在列出文件: {project['path']}")
    try:
        rel_files = sorted(_walk_files(project["path"], branch, token))
    except client.GitCodeError as e:
        return {"ok": False, "stage": "list_files", "error": str(e)}
    if not rel_files:
        return {"ok": True, "files": []}

    fetched = []
    total_size = 0
    for i, rel_path in enumerate(rel_files, 1):
        log(f"正在下载文件 ({i}/{len(rel_files)}): {rel_path}")
        source_path = f"{project['path']}/{rel_path}"
        try:
            content = client.get_file_text(
                *_source_repo(), source_path, branch, token)
        except client.GitCodeError as e:
            return {
                "ok": False,
                "stage": "download_file",
                "error": f"下载文件 {source_path} 失败: {e}",
            }

        content_bytes = content.encode("utf-8")
        total_size += len(content_bytes)
        if total_size > MAX_TOTAL_SIZE:
            return {
                "ok": False,
                "stage": "download_file",
                "error": (
                    f"累计文件大小 {total_size / 1024 / 1024:.1f} MB 超过上限 "
                    f"{MAX_TOTAL_SIZE / 1024 / 1024:.0f} MB"
                ),
            }

        # 校验路径穿越（拒绝绝对路径和 .. 相对路径）
        normalized = os.path.normpath(rel_path).replace("\\", "/")
        if normalized.startswith("..") or os.path.isabs(rel_path):
            return {
                "ok": False,
                "stage": "download_file",
                "error": f"非法的文件路径: {rel_path}",
            }

        fetched.append({
            "target_path": rel_path,
            "content_b64": base64.b64encode(content_bytes).decode("ascii"),
        })

    return {"ok": True, "files": fetched}


# ── 扫描对比 ──────────────────────────────────────────────────────────


def _compare_files(project, files, token):
    """对比仓库级目录的译稿与目标仓库内容，筛出有变更的文件。

    目标定位：org/repo/branch 取目录第 1/4/2 段。逐文件用 git blob sha
    与目标文件的 sha 比对；目标文件 sha 缺失时退化为下载全文做文本比对；
    目标路径已是同名目录时视为更新（提交阶段会记为单文件失败）。

    Returns:
        dict: {"status": "changed" | "unchanged" | "skipped", ...}
    """
    org = project["org"]
    repo = project["project"]
    branch = project["version"]

    try:
        branches = client.list_branches(org, repo, token)
    except client.GitCodeError as e:
        if e.status_code == 404:
            return {"status": "skipped",
                    "reason": f"目标仓库 {org}/{repo} 不存在或无权限"}
        raise
    names = {b.get("name") for b in branches}
    if branch not in names:
        return {"status": "skipped",
                "reason": f"目标仓库 {org}/{repo} 不存在分支 {branch}"}

    changed = []
    new_count = 0
    update_count = 0
    for f in files:
        content = base64.b64decode(f["content_b64"])
        existing = client.get_contents(org, repo, f["target_path"], branch, token)
        if existing is None:
            new_count += 1
            changed.append(f)
            continue
        if isinstance(existing, list):
            update_count += 1
            changed.append(f)
            continue
        remote_sha = existing.get("sha")
        if remote_sha:
            same = remote_sha == _git_blob_sha(content)
        else:
            remote_text = client.get_file_text(
                org, repo, f["target_path"], branch, token)
            same = remote_text.encode("utf-8") == content
        if not same:
            update_count += 1
            changed.append(f)

    if not changed:
        return {"status": "unchanged"}
    return {
        "status": "changed",
        "files": changed,
        "new_count": new_count,
        "update_count": update_count,
    }


def scan_changed(keyword="", token="", token_rules=None, log=print):
    """扫描全部（或按关键字过滤的）仓库级目录，找出有变更的译稿仓库。

    逐仓下载译稿并与目标仓库同路径文件做内容对比（blob sha 比对），
    仅「有新增或有差异」的仓库进入 changed 清单。单仓失败不中断：
    目标仓库/分支不存在记入 skipped，其余错误记入 errors，扫描继续。

    读取译稿权威仓库始终使用默认 token；访问目标仓库做对比的 Token
    由 resolve_token 按 token_rules 逐仓路由（权限管控）：配置了
    有效规则时，未命中规则的仓库记入 skipped 不予处理。

    Returns:
        dict with:
        - ok: bool
        - changed: [{project, org, repo, branch, files, new_count,
                     update_count}]（仅 ok=True）
        - unchanged: [无变更的仓库路径...]
        - skipped: [{path, reason}]
        - errors: [{path, error}]
    """
    keyword = (keyword or "").strip().lower()
    token = (token or "").strip()
    if not token:
        return {"ok": False, "stage": "validate", "error": "默认 Token 不能为空"}

    log("正在读取译稿权威仓库目录索引...")
    try:
        repo_info = client.get_repo(*_source_repo(), token)
        branch = repo_info.get("default_branch") or "master"
        paths = []
        top_dirs = _list_subdirs("", branch, token)
        for i, d in enumerate(top_dirs, 1):
            log(f"正在扫描产品目录 ({i}/{len(top_dirs)}): {d}")
            _collect_dirs(d, branch, token, 1, paths)
    except client.GitCodeError as e:
        return {"ok": False, "stage": "load_index", "error": str(e)}

    projects = [_to_project(p) for p in sorted(paths)]
    repos = [
        p for p in projects
        if p["is_repo"] and (not keyword or keyword in p["path"].lower())
    ]

    changed = []
    unchanged = []
    skipped = []
    errors = []
    # 配置了有效规则时只处理命中规则的仓库，规则外的一律跳过（权限管控）
    restrict = bool(_effective_rules(token_rules))
    for i, project in enumerate(repos, 1):
        log(f"正在扫描 ({i}/{len(repos)}): {project['path']}")
        cmp_token, rule_keyword = resolve_token(
            project["path"], token_rules, token)
        if restrict and not rule_keyword:
            skipped.append({"path": project["path"],
                            "reason": "未命中 Token 规则，按权限管控不处理"})
            continue
        if rule_keyword:
            log(f"  命中 Token 规则「{rule_keyword}」，使用规则 Token 对比")
        try:
            fetched = _fetch_repo_files(project, branch, token, log)
            if not fetched["ok"]:
                errors.append({
                    "path": project["path"],
                    "error": fetched.get("error", "下载译稿失败"),
                })
                continue
            if not fetched["files"]:
                continue
            cmp_result = _compare_files(project, fetched["files"], cmp_token)
        except client.GitCodeError as e:
            errors.append({"path": project["path"], "error": str(e)})
            continue

        status = cmp_result["status"]
        if status == "changed":
            changed.append({
                "project": project,
                "org": project["org"],
                "repo": project["project"],
                "branch": project["version"],
                "files": cmp_result["files"],
                "new_count": cmp_result["new_count"],
                "update_count": cmp_result["update_count"],
            })
        elif status == "skipped":
            skipped.append({"path": project["path"],
                            "reason": cmp_result["reason"]})
        else:
            unchanged.append(project["path"])

    return {
        "ok": True,
        "changed": changed,
        "unchanged": unchanged,
        "skipped": skipped,
        "errors": errors,
    }


# ── Fork 与分支管理 ───────────────────────────────────────────────────


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
    全量同步（不传 branch）——全量同步会把上游新增分支补进 Fork。
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


# ── 幂等提交 ──────────────────────────────────────────────────────────


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
    复用或创建固定分支（translator/auto-<目标分支>）→ 写入文件
    （与分支现有内容相同的跳过）→ 去重后创建 PR。

    幂等语义：同一仓库重复运行时，已开 PR 不重复创建，新变更以
    新 commit 进入同一 PR；分支内容已是最新时不产生空 commit。

    Returns:
        dict with ok, pr_url, pr_number, pr_existed, head_branch,
        succeeded, unchanged, failed / stage, error
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

    # ── 阶段 4：写入文件（与分支现有内容相同的跳过，避免空 commit）──
    succeeded = []
    unchanged = []
    failed = []
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

    if not succeeded and not unchanged:
        # 没有任何文件写入成功且分支上也没有可用内容：不能创建空 PR
        return {
            "ok": False,
            "stage": "write",
            "error": "所有文件均写入失败，未创建 PR",
            "head_branch": head_branch,
            "succeeded": succeeded,
            "failed": failed,
        }
    if not succeeded:
        log("分支内容已是最新，无需写入")

    # ── 阶段 5：PR 去重——已存在同一 head/base 的 open PR 时不重复创建 ──
    head = f"{fork_owner}:{head_branch}"
    log("正在检查是否已有未合并的 PR...")
    try:
        open_prs = client.list_pull_requests(
            org, repo, token, state="open", head=head, base=branch)
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
            "failed": failed,
        }

    # ── 阶段 6：创建 Pull Request ──
    pr_files = succeeded or [
        {"path": p, "action": "update"} for p in unchanged]
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
        return {
            "ok": False,
            "stage": "create_pr",
            "error": str(e),
            "head_branch": head_branch,
            "succeeded": succeeded,
            "unchanged": unchanged,
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
        "failed": failed,
    }


def submit_all(changed, token, token_rules=None, log=print):
    """对有变更的仓库逐个幂等提交 PR，单仓失败不中断。

    每个仓库使用的 Token 由 resolve_token 按 token_rules 路由
    （权限管控）：配置了有效规则时，未命中规则的仓库记为失败
    不予提交；未配置规则时全部使用默认 token。

    Args:
        changed: scan_changed 返回的 changed 清单，
                 每项含 org/repo/branch/files/project
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
            item_token, log)
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
