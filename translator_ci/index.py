"""来源仓库（译稿权威仓库 / Fork）的同步与目录索引。

sync_source:   来源为 Fork 时，扫描前同步其与上游
_list_subdirs: 列出指定目录下的子目录名（过滤掉文件）
_walk_files:   递归列出目录下全部文件（携带来源 blob sha，路径校验前置）
_collect_dirs: 递归收集所有（子树内）包含文件的目录路径（MAX_DEPTH 截断告警）
_to_project:   目录路径按固定层级解析为项目描述
"""

import os
import time

from translator_ci import client
from translator_ci.common import (
    _source_repo, SOURCE_ORG, SOURCE_REPO, MAX_DEPTH,
)


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


def _list_subdirs(path, ref, token):
    """列出权威仓库指定目录下的子目录名（过滤掉文件）。"""
    entries = client.list_dir(*_source_repo(), path, ref, token)
    return [e["name"] for e in entries if e.get("type") == "dir"]


def _walk_files(path, ref, token, prefix=""):
    """递归列出权威仓库指定目录下的全部文件。

    Returns:
        list: [{"path": 相对路径, "sha": 来源 blob sha}]。

    路径校验前置：在此处拒绝绝对路径与 .. 相对路径（下载前拦截，
    避免下载越界内容）；目录条目未携带 sha 时该项为 None，对比阶段
    自动退化为下载内容兜底。
    """
    files = []
    entries = client.list_dir(*_source_repo(), path, ref, token)
    for e in entries:
        rel = f"{prefix}{e['name']}"
        if e.get("type") == "dir":
            files.extend(_walk_files(e["path"], ref, token, prefix=rel + "/"))
        else:
            normalized = os.path.normpath(rel).replace("\\", "/")
            if normalized.startswith("..") or os.path.isabs(rel):
                raise client.GitCodeError(400, f"非法的文件路径: {rel}")
            files.append({"path": rel, "sha": e.get("sha")})
    return files


def _collect_dirs(path, ref, token, depth, out, log=print):
    """递归收集目录树中所有（子树内）包含文件的目录路径。

    目录深度超过 MAX_DEPTH 的部分不会递归（避免索引过深），
    截断时记录告警，便于发现目录树比预期更深导致的遗漏。

    Returns:
        int: 该目录子树内的文件总数（用于判断目录是否有效）。
    """
    entries = client.list_dir(*_source_repo(), path, ref, token)
    count = 0
    for e in entries:
        if e.get("type") == "dir":
            if depth < MAX_DEPTH:
                count += _collect_dirs(e["path"], ref, token, depth + 1,
                                       out, log)
            else:
                log(f"  [警告] 目录深度超过上限 {MAX_DEPTH}，跳过其子树: "
                    f"{e['path']}")
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
