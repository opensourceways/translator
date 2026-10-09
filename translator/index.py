"""来源仓库（译稿权威仓库 / Fork）的同步与目录索引。

sync_source:       来源为 Fork 时，扫描前同步其与上游
_list_subdirs:     列出指定目录下的子目录名（过滤掉文件）
_walk_files:       递归列出目录下全部文件（携带来源 blob sha，路径校验前置）
_collect_dirs:     递归收集所有（子树内）包含文件的目录路径（不限制嵌套深度）
_to_project:       目录路径按固定层级解析为项目描述
_target_rel_path:  仓库级目录下相对路径 → 目标仓库内路径（按仓库规则改写）
"""

import os
import time

from translator import client
from translator.common import (
    _source_repo, SOURCE_ORG, SOURCE_REPO,
)

# 各产品组织在权威仓中的「仓库级目录」段数（org/.../仓库名）：
#   Ascend: org/版本/产品/仓库 → 4 段（MindStudio 为产品层，msprof 等为仓库名）
#   CANN:   org/版本/仓库      → 3 段（hccl/sip 等直接是仓库名，与 CANN 交付说明一致）
# 目标仓库 = 组织/最后一段；目标分支 = 第 2 段（版本）；目标路径 = 仓库级之后。
# 未登记的组织按默认 3 段（org/版本/仓库）处理。
REPO_DEPTH_BY_ORG = {"Ascend": 4}
DEFAULT_REPO_DEPTH = 3

# 各目标仓库「仓库级目录下相对路径 → 仓库内目标路径」的改写规则
# （按 org/repo 匹配，与版本无关）。未登记的仓库直连——源目录结构与
# 目标仓库内容根结构一致（如 sip/hccl 的 docs/en/... 原样透传）。
#   prepend:  目标路径前缀。CANN/docs 仓内容根为 docs/（按 docs/zh、
#             docs/en 分语言），源目录缺该层，需补前缀。
#   en_flat:  拍平首段 en/ 语言目录。CANN/release-management 仓只有
#             master 分支（版本号是文件夹而非分支），无语言目录，英文
#             以 <版本>/<文件>_en.md 与中文并存：剥离 en/ 并在扩展名前
#             补 _en（文件名已带 _en 则不重复）；不在 en/ 下的原样透传。
TARGET_PATH_RULES = {
    "CANN/docs": {"prepend": "docs/"},
    "CANN/release-management": {"en_flat": True},
}


def _repo_depth(path):
    """按路径所属组织返回仓库级目录段数。"""
    org = path.split("/", 1)[0]
    return REPO_DEPTH_BY_ORG.get(org, DEFAULT_REPO_DEPTH)


def _target_rel_path(org, repo, rel):
    """把仓库级目录下的来源相对路径改写为目标仓库内路径。

    未登记 org/repo 的仓库直连（rel 原样返回）；登记的按
    TARGET_PATH_RULES 改写（见规则表注释）。
    """
    rule = TARGET_PATH_RULES.get(f"{org}/{repo}", {})
    path = rel
    if rule.get("en_flat") and path.startswith("en/"):
        path = path[len("en/"):]
        stem, dot, ext = path.rpartition(".")
        if dot and not stem.endswith("_en"):
            path = f"{stem}_en.{ext}"
    prefix = rule.get("prepend")
    if prefix:
        path = prefix + path
    return path


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


def _collect_dirs(path, ref, token, out, log=print):
    """递归收集目录树中所有（子树内）包含文件的目录路径。

    不限制嵌套深度：权威仓库的译稿目录树深度不固定（当前实测最深 8 级），
    人为设深限可能漏掉「文件只存在于深层子树」的仓库级目录；目录树规模
    有限（几十到几百个目录），完整递归即可。

    Returns:
        int: 该目录子树内的文件总数（用于判断目录是否有效）。
    """
    entries = client.list_dir(*_source_repo(), path, ref, token)
    count = 0
    for e in entries:
        if e.get("type") == "dir":
            count += _collect_dirs(e["path"], ref, token, out, log)
        else:
            count += 1
    if path and count:
        out.append(path)
    return count


def _to_project(path):
    """把目录路径按组织规定的层级解析为项目描述。

    仓库级目录段数由组织决定（REPO_DEPTH_BY_ORG）：Ascend 为
    org/版本/产品/仓库（4 段），CANN 为 org/版本/仓库（3 段）。
    org 取第 1 段、版本取第 2 段（即目标分支），仓库名取仓库级最后一段，
    仓库级之后为目标路径。is_repo 标记是否为完整的仓库级目录——
    只有仓库级目录参与扫描提交。
    """
    segs = path.split("/")
    depth = _repo_depth(path)
    return {
        "org": segs[0] if segs else "",
        "version": segs[1] if len(segs) > 1 else "",
        "product": segs[2] if depth == 4 and len(segs) > 2 else "",
        "project": segs[depth - 1] if len(segs) >= depth else "",
        "path": path,
        "is_repo": len(segs) == depth,
    }


def _repo_dirs_from_tree(tree):
    """从本地文件映射（localgit.tree_map，path → blob sha）提取仓库级目录。

    等价于 API 版 _list_subdirs + _collect_dirs，但只占 1 次本地命令。
    仓库级目录为「组织/.../仓库名」固定段数（由组织决定）；只以「严格深于
    仓库级」的文件推导目录，避免把 org/版本 下的散文件（如
    CANN/9.1.0/README.md）误判为仓库。去重后排序。
    """
    dirs = set()
    for p in tree:
        segs = p.split("/")
        depth = _repo_depth(p)
        if len(segs) > depth:
            dirs.add("/".join(segs[:depth]))
    return sorted(dirs)


def _repo_files_from_tree(tree, repo_path):
    """从本地文件映射提取仓库级目录下的文件（相对路径 + blob sha）。

    路径校验与 _walk_files 一致：拒绝绝对路径与 .. 相对路径。
    本地映射只含 blob，天然不存在「目录条目缺 sha」的情况，sha 必有值。
    """
    prefix = repo_path + "/"
    files = []
    for p, sha in tree.items():
        if not p.startswith(prefix):
            continue
        rel = p[len(prefix):]
        normalized = os.path.normpath(rel).replace("\\", "/")
        if normalized.startswith("..") or os.path.isabs(rel):
            raise client.GitCodeError(400, f"非法的文件路径: {rel}")
        files.append({"path": rel, "sha": sha})
    return files
