"""扫描译稿仓库并与目标仓库对比，筛出有变更的仓库。

_fetch_repo_files: 列出仓库级目录的译稿文件（带来源 blob sha，不下载内容）
_compare_files:    逐文件对比来源与目标内容，筛出有变更的文件（按需下载）
scan_changed:      扫描全部（或按关键字过滤的）仓库级目录 → 有变更清单

读取译稿权威仓库始终使用默认 token；访问目标仓库做对比的 Token
由 resolve_token 按 token_rules 逐仓路由（权限管控）：配置了有效
规则时，未命中规则的仓库记入 skipped 不予处理。
"""

import base64

from translator_ci import client
from translator_ci.common import _source_repo, MAX_TOTAL_SIZE, _git_blob_sha
from translator_ci.index import _list_subdirs, _collect_dirs, _to_project, _walk_files
from translator_ci.rules import resolve_token, _effective_rules


def _fetch_repo_files(project, branch, token, log):
    """列出仓库级目录的全部译稿文件（携带来源 blob sha，不下载内容）。

    目标路径为仓库级目录内的相对路径（即目标仓库中的真实存放路径）；
    路径校验已在 _walk_files 阶段前置完成。文件内容推迟到「确认有变更」
    后再按需下载（见 _compare_files），避免为无变更文件做无谓传输。

    Returns:
        dict: {"ok": bool, "files": [{target_path, sha}], "error": ...}
    """
    log(f"正在列出文件: {project['path']}")
    try:
        rel_files = sorted(_walk_files(project["path"], branch, token),
                           key=lambda f: f["path"])
    except client.GitCodeError as e:
        return {"ok": False, "stage": "list_files", "error": str(e)}
    if not rel_files:
        return {"ok": True, "files": []}
    return {"ok": True, "files": [
        {"target_path": f["path"], "sha": f.get("sha")} for f in rel_files
    ]}


def _compare_files(project, files, cmp_token, source_token, log=print):
    """对比仓库级目录的译稿与目标仓库内容，筛出有变更的文件。

    目标定位：org/repo/branch 取目录第 1/4/2 段。优先用「来源目录条目的
    blob sha」与目标文件 sha 直接比对，两者一致即未变更，无需下载来源
    内容（来源条目未携带 sha、或目标 sha 缺失时才下载全文做 blob sha /
    文本兜底比对）。变更文件按需下载并携带内容，供提交阶段写入使用。

    Returns:
        dict: {"status": "changed" | "unchanged" | "skipped", ...}
    """
    org = project["org"]
    repo = project["project"]
    branch = project["version"]

    try:
        branches = client.list_branches(org, repo, cmp_token)
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
    total_size = 0

    def download(rel_path):
        """按需下载来源仓库单个文件内容（累计大小受 MAX_TOTAL_SIZE 约束）。

        来源仓库始终用默认 Token 读取（与扫描一致），规则 Token 只用于
        访问目标仓库。
        """
        nonlocal total_size
        source_path = f"{project['path']}/{rel_path}"
        log(f"  下载译稿: {rel_path}")
        try:
            # 按字节读取：译稿可能混有图片等二进制文件，不能按文本解码
            content = client.get_file_bytes(
                *_source_repo(), source_path, branch, source_token)
        except client.GitCodeError as e:
            raise client.GitCodeError(
                e.status_code, f"下载文件 {source_path} 失败: {e.message}")
        total_size += len(content)
        if total_size > MAX_TOTAL_SIZE:
            raise client.GitCodeError(
                200, f"累计文件大小 {total_size / 1024 / 1024:.1f} MB 超过上限 "
                     f"{MAX_TOTAL_SIZE / 1024 / 1024:.0f} MB")
        return base64.b64encode(content).decode("ascii")

    for f in files:
        existing = client.get_contents(
            org, repo, f["target_path"], branch, cmp_token)
        if existing is None:
            new_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["target_path"]),
            })
            continue
        if isinstance(existing, list):
            # 目标路径已是同名目录：视为更新（提交阶段会记为单文件失败）
            update_count += 1
            changed.append({
                "target_path": f["target_path"],
                "content_b64": download(f["target_path"]),
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
                "content_b64": download(f["target_path"]),
            })
            continue
        # 任一 sha 缺失：下载来源内容做 blob sha / 文本兜底比对
        b64 = download(f["target_path"])
        content = base64.b64decode(b64)
        if remote_sha:
            same = remote_sha == _git_blob_sha(content)
        else:
            remote_bytes = client.get_file_bytes(
                org, repo, f["target_path"], branch, cmp_token)
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


def scan_changed(keyword="", token="", token_rules=None, log=print):
    """扫描全部（或按关键字过滤的）仓库级目录，找出有变更的译稿仓库。

    逐仓下载译稿并与目标仓库同路径文件做内容对比（blob sha 比对），
    仅「有新增或有差异」的仓库进入 changed 清单。单仓失败不中断：
    目标仓库/分支不存在记入 skipped，其余错误记入 errors，扫描继续。

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
            _collect_dirs(d, branch, token, 1, paths, log)
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
            cmp_result = _compare_files(project, fetched["files"], cmp_token,
                                        token, log)
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
