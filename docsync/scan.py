"""扫描译稿仓库并与目标仓库对比，筛出有变更的仓库。

_fetch_repo_files: 列出仓库级目录的译稿文件（带来源 blob sha，不下载内容）
_compare_files:    逐文件对比来源与目标内容，筛出有变更的文件（按需下载）
_local_target_tree: 本地化模式下克隆目标仓库、生成本地文件映射
scan_changed:      扫描全部（或按关键字过滤的）仓库级目录 → 有变更清单

两种工作模式：
- API 模式（默认）：索引/列文件/对比全部走 GitCode API，进程级限流兜底。
- 本地化模式（source_repo_dir 非空）：权威仓库已在 agent 上克隆，索引、
  列文件与来源内容读取全走本地 git；目标仓库按需浅克隆后本地对比，
  彻底绕开 API 限流，API 只保留建 PR 等少量写操作。

读取译稿权威仓库始终使用默认 token；访问目标仓库做对比的 Token
由 resolve_token 按 token_rules 逐仓路由（权限管控）：配置了有效
规则时，未命中规则的仓库记入 skipped 不予处理。

拉黑名单（权威仓库 blacklist.json）：目录路径命中名单的仓库直接记入
skipped，不做任何对比请求（业务侧临时不想处理的仓库用，无需改代码）。

开放白名单（权威仓库 whitelist.json）：名单非空时只处理命中的仓库，
名单外的一律记入 skipped，不做任何对比请求（范围管控，如「功能先只对
某仓库开放」）；名单为空时不限制开放范围。
"""

import base64
import os

from docsync import client, localgit
from docsync.common import (
    _source_repo, MAX_FILE_SIZE, MAX_TOTAL_SIZE, _git_blob_sha)
from docsync.index import (
    _list_subdirs, _collect_dirs, _to_project, _walk_files,
    _repo_dirs_from_tree, _repo_files_from_tree, _target_rel_path,
)
from docsync.rules import resolve_token, _effective_rules


def _fetch_repo_files(project, branch, token, log, tree=None):
    """列出仓库级目录的全部译稿文件（携带来源 blob sha，不下载内容）。

    每个文件带两个路径：path 为仓库级目录内的来源相对路径（下载译稿
    时拼接 project['path'] 用）；target_path 为目标仓库中的真实存放
    路径，按 index.TARGET_PATH_RULES 改写（未登记的仓库直连）。
    路径校验已在 _walk_files / _repo_files_from_tree 阶段前置完成。
    文件内容推迟到「确认有变更」后再按需下载（见 _compare_files），
    避免为无变更文件做无谓传输。tree 非空时从本地文件映射提取
    （本地化模式），否则走 API 逐目录列举。

    Returns:
        dict: {"ok": bool, "files": [{path, target_path, sha}], "error": ...}
    """
    log(f"正在列出文件: {project['path']}")
    try:
        if tree is not None:
            rel_files = sorted(
                _repo_files_from_tree(tree, project["path"]),
                key=lambda f: f["path"])
        else:
            rel_files = sorted(_walk_files(project["path"], branch, token),
                               key=lambda f: f["path"])
    except client.GitCodeError as e:
        return {"ok": False, "stage": "list_files", "error": str(e)}
    if not rel_files:
        return {"ok": True, "files": []}
    org, repo = project["org"], project["project"]
    return {"ok": True, "files": [
        {"path": f["path"],
         "target_path": _target_rel_path(org, repo, f["path"]),
         "sha": f.get("sha")} for f in rel_files
    ]}


def _compare_files(project, files, cmp_token, source_token, source_branch,
                   log=print, target_tree=None, source_repo_dir=None):
    """对比仓库级目录的译稿与目标仓库内容，筛出有变更的文件。

    目标定位：org/repo/branch 按组织层级取（见 index._to_project，Ascend
    为第 1/4/2 段，CANN 等为第 1/3/2 段）；目标路径取 target_path
    （按 index.TARGET_PATH_RULES 改写后的真实存放路径），下载来源内容
    用 path（仓库级目录内相对路径）拼接源目录。下载来源内容时使用
    权威仓库分支 source_branch（与索引/列目录一致，勿混用目标分支）。
    优先用「来源目录条目的 blob sha」与目标文件 sha 直接比对，两者一致
    即未变更，无需下载来源内容（来源条目未携带 sha、或目标 sha 缺失时
    才下载全文做 blob sha / 文本兜底比对）。变更文件按需下载并携带内容，
    供提交阶段写入使用。

    本地化模式（target_tree / source_repo_dir 非空）：目标仓库已在本地
    克隆，用本地 blob sha 映射直接比对，一条 API 都不发；来源内容从本地
    克隆按 sha 读取。target_tree 为 None 时走 API 逐文件对比（回退）。

    Returns:
        dict: {"status": "changed" | "unchanged" | "skipped", ...}
    """
    org = project["org"]
    repo = project["project"]
    target_branch = project["version"]

    changed = []
    new_count = 0
    update_count = 0
    total_size = 0

    def download(rel_path, sha=None):
        """按需获取来源仓库单个文件内容（单文件与累计大小受约束）。

        来源仓库始终用默认 Token 读取（与扫描一致），规则 Token 只用于
        访问目标仓库。本地化模式下从本地克隆按 sha 读取（git cat-file，
        部分克隆时按需补取 blob），否则走 API。单文件超 MAX_FILE_SIZE
        立即拦截；累计超 MAX_TOTAL_SIZE 拦截整批。
        """
        nonlocal total_size
        source_path = f"{project['path']}/{rel_path}"
        if source_repo_dir:
            if not sha:
                raise client.GitCodeError(
                    200, f"本地化模式缺少来源 blob sha: {rel_path}")
            log(f"  读取本地译稿: {rel_path}")
            try:
                content = localgit.cat_blob(source_repo_dir, sha)
            except client.GitCodeError as e:
                raise client.GitCodeError(
                    e.status_code, f"读取文件 {source_path} 失败: {e}")
        else:
            log(f"  下载译稿: {rel_path}")
            try:
                # 按字节读取：译稿可能混有图片等二进制文件，不能按文本解码。
                # 注意必须用权威仓库分支 source_branch，而非目标分支 target_branch
                content = client.get_file_bytes(
                    *_source_repo(), source_path, source_branch, source_token)
            except client.GitCodeError as e:
                raise client.GitCodeError(
                    e.status_code, f"下载文件 {source_path} 失败: {e.message}")
        if len(content) > MAX_FILE_SIZE:
            raise client.GitCodeError(
                200, f"文件 {rel_path} 大小 {len(content) / 1024 / 1024:.1f} MB "
                     f"超过单文件上限 {MAX_FILE_SIZE / 1024 / 1024:.0f} MB")
        total_size += len(content)
        if total_size > MAX_TOTAL_SIZE:
            raise client.GitCodeError(
                200, f"累计文件大小 {total_size / 1024 / 1024:.1f} MB 超过上限 "
                     f"{MAX_TOTAL_SIZE / 1024 / 1024:.0f} MB")
        return base64.b64encode(content).decode("ascii")

    if target_tree is not None:
        # ── 本地化对比：目标仓库已在本地克隆，直接用 blob sha 比对 ──
        # 分支存在性由克隆成功隐含（--branch 指定分支不存在即克隆失败），
        # 无需再查分支列表；目标路径为同名目录时 ls-tree 不含该路径，
        # 按「新文件」处理（提交阶段写入时若冲突记单文件失败）。
        for f in files:
            remote_sha = target_tree.get(f["target_path"])
            if remote_sha is None:
                new_count += 1
                changed.append({
                    "target_path": f["target_path"],
                    "content_b64": download(f["path"], f.get("sha")),
                })
                continue
            if remote_sha == f.get("sha"):
                continue
            update_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["path"], f.get("sha")),
            })
        if not changed:
            return {"status": "unchanged"}
        return {
            "status": "changed",
            "files": changed,
            "new_count": new_count,
            "update_count": update_count,
        }

    # ── API 路径（原有逻辑，保留为回退）──
    try:
        branches = client.list_branches(org, repo, cmp_token)
    except client.GitCodeError as e:
        if e.status_code == 404:
            return {"status": "skipped",
                    "reason": f"目标仓库 {org}/{repo} 不存在或无权限"}
        raise
    names = {b.get("name") for b in branches}
    if target_branch not in names:
        return {"status": "skipped",
                "reason": f"目标仓库 {org}/{repo} 不存在分支 {target_branch}"}

    for f in files:
        existing = client.get_contents(
            org, repo, f["target_path"], target_branch, cmp_token)
        if existing is None:
            new_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["path"], f.get("sha")),
            })
            continue
        if isinstance(existing, list):
            # 目标路径已是同名目录：视为更新（提交阶段会记为单文件失败）
            update_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["path"], f.get("sha")),
            })
            continue
        remote_sha = existing.get("sha")
        if remote_sha and f.get("sha"):
            # 源/目标 blob sha 直接比对，一致即未变更，免下载
            if remote_sha == f["sha"]:
                continue
            update_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["path"], f.get("sha")),
            })
            continue
        # 任一 sha 缺失：下载来源内容做 blob sha / 文本兜底比对
        b64 = download(f["path"], f.get("sha"))
        content = base64.b64decode(b64)
        if remote_sha:
            same = remote_sha == _git_blob_sha(content)
        else:
            remote_bytes = client.get_file_bytes(
                org, repo, f["target_path"], target_branch, cmp_token)
            same = remote_bytes == content
        if not same:
            update_count += 1
            changed.append({"target_path": f["target_path"],
                            "content_b64": b64})

    if not changed:
        return {"status": "unchanged"}
    return {
        "status": "changed",
        "files": changed,
        "new_count": new_count,
        "update_count": update_count,
    }


def _local_target_tree(project, cmp_token, clone_root, log):
    """克隆目标仓库到本地并返回其指定分支的文件映射（path → blob sha）。

    克隆失败（仓库不存在 / 无权限 / 目标分支不存在等）返回 None，由
    _compare_files 回退到 API 路径处理（保持原有的跳过/报错语义）。
    使用规则 Token（或默认 Token）克隆，与 API 访问目标仓库的口径一致；
    部分克隆只拉目录树，对比无需 blob。
    """
    org = project["org"]
    repo = project["project"]
    target_branch = project["version"]
    dest = os.path.join(clone_root, f"{org}__{repo}")
    log(f"  克隆目标仓库本地索引: {org}/{repo}@{target_branch}")
    try:
        localgit.clone(org, repo, target_branch, cmp_token, dest, log=log)
        return localgit.tree_map(dest)
    except client.GitCodeError as e:
        log(f"  目标仓库本地克隆失败，回退 API 对比: {e}")
        return None


def _path_matched(path, entries):
    """判断仓库目录路径是否命中名单（等于条目或以条目为前缀）。

    黑/白名单共用同一套目录树匹配：条目为仓库级目录（如
    CANN/9.1.0/sip）时精确命中，条目为高层目录（如 CANN/9.1.0）时
    其下所有仓库级目录都命中。
    """
    for entry in entries or []:
        if path == entry or path.startswith(entry + "/"):
            return True
    return False


def scan_changed(keyword="", token="", token_rules=None, blacklist=None,
                 whitelist=None, log=print, source_repo_dir=None,
                 clone_root=None, diff_before=None, diff_after=None):
    """扫描全部（或按关键字过滤的）仓库级目录，找出有变更的译稿仓库。

    逐仓下载译稿并与目标仓库同路径文件做内容对比（blob sha 比对），
    仅「有新增或有差异」的仓库进入 changed 清单。单仓失败不中断：
    目标仓库/分支不存在记入 skipped，其余错误记入 errors，扫描继续。

    blacklist 为拉黑名单（权威仓库 blacklist.json 的目录路径列表）：
    命中名单的仓库直接记入 skipped，不做对比也不建 PR。

    whitelist 为开放白名单（权威仓库 whitelist.json 的目录路径列表）：
    非空时只处理命中名单的仓库，名单外的直接记入 skipped（范围管控，
    如「功能先只对某仓库开放」）；为空时不限制开放范围。拉黑优先——
    名单外跳过，名单内但被拉黑的仍跳过。

    本地化模式（source_repo_dir 非空，通常由 Jenkinsfile 克隆权威仓库
    后通过 GITCODE_LOCAL_SOURCE_DIR 注入）：索引与列文件走本地 git，
    目标仓库按需浅克隆（clone_root 指定目录）后本地对比，彻底绕开 API
    限流；clone_root 未设置时仅索引本地化、对比回退 API。

    增量模式（diff_before/diff_after 均非空，来自 push webhook 的
    before/after sha，仅本地化模式支持）：用 git diff 算出本次推送的
    变更路径，只处理「变更路径落在其目录下」的仓库——非译稿文件
    （根目录 README 等）的变更不触发任何仓库处理；翻译团队未提交
    新译稿的仓库（上游单方面维护导致内容差异）也不会被无关推送
    再次触发。回退全量的场景：diff 失败（sha 不可达等）、变更路径
    命中根目录配置文件（blacklist.json / token_rules.json /
    whitelist.json，配置变更影响全局，需全量补扫）。

    Returns:
        dict with:
        - ok: bool
        - changed: [{project, org, repo, branch, files,
                     new_count, update_count}]（仅 ok=True）
        - unchanged: [无变更的仓库路径...]
        - skipped: [{path, reason}]
        - errors: [{path, error}]
        - incremental: bool（实际是否按增量模式运行）
    """
    keyword = (keyword or "").strip().lower()
    token = (token or "").strip()
    if not token:
        return {"ok": False, "stage": "validate", "error": "默认 Token 不能为空"}

    local_mode = bool(source_repo_dir)
    # 根目录配置文件：变更影响全局，命中即回退全量扫描
    CONFIG_FILES = ("blacklist.json", "token_rules.json", "whitelist.json",
                    "README.md")

    incremental = False
    changed_paths = set()
    if diff_before and diff_after and local_mode:
        try:
            localgit.fetch_commit(source_repo_dir, diff_before)
            changed_paths = localgit.diff_paths(
                source_repo_dir, diff_before, diff_after)
            incremental = True
            log(f"增量模式：本次推送变更 {len(changed_paths)} 个路径")
            if changed_paths & set(CONFIG_FILES):
                log("  变更命中根目录配置文件，回退全量扫描")
                incremental = False
                changed_paths = set()
        except client.GitCodeError as e:
            log(f"增量 diff 失败（{e}），回退全量扫描")
            incremental = False
            changed_paths = set()
    elif diff_before and diff_after:
        log("增量参数需要本地化模式（GITCODE_LOCAL_SOURCE_DIR），忽略")

    log("正在读取译稿权威仓库目录索引...")
    try:
        if local_mode:
            log(f"使用本地克隆目录索引: {source_repo_dir}")
            tree = localgit.tree_map(source_repo_dir)
            paths = _repo_dirs_from_tree(tree)
            branch = "HEAD"
        else:
            tree = None
            repo_info = client.get_repo(*_source_repo(), token)
            branch = repo_info.get("default_branch") or "master"
            paths = []
            top_dirs = _list_subdirs("", branch, token)
            for i, d in enumerate(top_dirs, 1):
                log(f"正在扫描产品目录 ({i}/{len(top_dirs)}): {d}")
                _collect_dirs(d, branch, token, paths, log)
    except client.GitCodeError as e:
        return {"ok": False, "stage": "load_index", "error": str(e)}

    projects = [_to_project(p) for p in sorted(paths)]
    repos = [
        p for p in projects
        if p["is_repo"] and (not keyword or keyword in p["path"].lower())
    ]

    if incremental:
        # 只保留「本次推送变更路径落在其目录下」的仓库。
        # 仓库目录本身的直接变更（目录更名等）不算命中——目录结构由
        # _to_project 解析，改名后是另一个仓库路径，按新路径正常处理。
        repos = [
            p for p in repos
            if any(cp == p["path"] or cp.startswith(p["path"] + "/")
                   for cp in changed_paths)
        ]
        log(f"增量过滤：{len(repos)} 个仓库目录命中本次推送变更")

    changed = []
    unchanged = []
    skipped = []
    errors = []
    # 配置了有效规则时只处理命中规则的仓库，规则外的一律跳过（权限管控）
    restrict = bool(_effective_rules(token_rules))
    for i, project in enumerate(repos, 1):
        log(f"正在扫描 ({i}/{len(repos)}): {project['path']}")
        if whitelist and not _path_matched(project["path"], whitelist):
            skipped.append({"path": project["path"],
                            "reason": "不在开放白名单中，暂不处理"})
            continue
        if _path_matched(project["path"], blacklist):
            skipped.append({"path": project["path"],
                            "reason": "在拉黑名单中，业务侧暂不处理"})
            continue
        cmp_token, rule_keyword = resolve_token(
            project["path"], token_rules, token)
        if restrict and not rule_keyword:
            skipped.append({"path": project["path"],
                            "reason": "未命中 Token 规则，按权限管控不处理"})
            continue
        if rule_keyword:
            log(f"  命中 Token 规则「{rule_keyword}」，使用规则 Token 对比")
        try:
            fetched = _fetch_repo_files(
                project, branch, token, log, tree=tree if local_mode else None)
            if not fetched["ok"]:
                errors.append({
                    "path": project["path"],
                    "error": fetched.get("error", "下载译稿失败"),
                })
                continue
            if not fetched["files"]:
                continue
            target_tree = None
            if local_mode and clone_root:
                target_tree = _local_target_tree(
                    project, cmp_token, clone_root, log)
            cmp_result = _compare_files(
                project, fetched["files"], cmp_token, token, branch, log,
                target_tree=target_tree,
                source_repo_dir=source_repo_dir if local_mode else None)
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
        "incremental": incremental,
    }
