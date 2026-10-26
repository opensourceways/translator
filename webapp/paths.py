"""权威仓文件路径解析：路径 → 目标仓库/分支/目标内路径。

权威仓目录层级由组织决定（与引擎 index.REPO_DEPTH_BY_ORG 同一口径）：
- Ascend（4 段仓库级目录）：org/产品/版本/仓库/... 或历史写法
  org/版本/产品/仓库/...，两种布局兼容
- CANN 等（默认 3 段）：org/版本/仓库/...
- 未登记组织且第 2 段不像版本时，按 org/产品/版本/仓库 的 4 段布局兜底

版本段在仓库级目录之前按特征识别（master/main/版本号/发布分支），
识别失败直接拒绝，不做猜测。
"""

import re

from translator.index import _repo_depth, DEFAULT_REPO_DEPTH, _target_rel_path

# 版本/分支段特征：master、main、26.2.0、v3.2.0、branch_v26.1.0_更新、
# release_v26.2.0、core_r0.18.0、v2.7.1-26.1.0-en-update-xxxx 等。
_VERSION_RE = re.compile(
    r"^(?:master|main|(?:branch|release|core)_[^/]+|v?\d[\w.\-]*)$",
    re.IGNORECASE)


def _looks_like_version(segment):
    """判断路径段是否像目标分支名（版本号或 master/main）。"""
    return bool(_VERSION_RE.match(segment or ""))


def parse_source_path(path):
    """解析单个权威仓文件路径。

    Args:
        path: 形如 ``Ascend/MindStudio/26.2.0/msprof/docs/en/a.md`` 的
            权威仓内相对路径（必须指向文件，不能是目录）。

    Returns:
        dict: source_path / org / repo / branch / target_path。

    Raises:
        ValueError: 空路径、绝对路径、含 ..、层级不足、目录路径或无法识别
            版本段时抛出。
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("路径不能为空")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"路径必须为权威仓内相对路径: {raw!r}")
    if "\\" in raw:
        raise ValueError(f"路径不能包含反斜杠: {raw!r}")

    segments = raw.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"路径包含空段、. 或 ..: {raw!r}")

    depth = _repo_depth(raw)
    version_index = None
    # 仓库段（depth-1）必须是仓库名，版本段只能出现在它之前
    for index in range(1, min(depth - 1, len(segments))):
        if _looks_like_version(segments[index]):
            version_index = index
    if version_index is None and depth == DEFAULT_REPO_DEPTH \
            and len(segments) > 3 and _looks_like_version(segments[2]) \
            and not _looks_like_version(segments[1]):
        # 未登记组织的 4 段布局：org/产品/版本/仓库
        depth, version_index = 4, 2
    if version_index is None:
        raise ValueError(
            f"无法识别版本/分支段（组织 {segments[0]!r} 的仓库级目录前应有"
            f"版本段）: {raw!r}")

    if len(segments) <= depth:
        raise ValueError(f"路径必须指向文件，不能是仓库目录: {raw!r}")

    org = segments[0]
    branch = segments[version_index]
    repo = segments[depth - 1]
    rel = "/".join(segments[depth:])
    if not org or not repo or not branch or not rel:
        raise ValueError(f"路径缺少组织/仓库/分支/文件段: {raw!r}")

    return {
        "source_path": raw,
        "org": org,
        "repo": repo,
        "branch": branch,
        "target_path": _target_rel_path(org, repo, rel),
    }


def group_paths(paths):
    """按 (org, repo, branch) 分组，供“同仓同分支合并为一个 PR”。

    Args:
        paths: 权威仓文件路径列表（调用方已去重）。

    Returns:
        list[dict]: [{org, repo, branch, source_paths,
                      files: [{source_path, target_path}]}]

    Raises:
        ValueError: 任一格式非法，或同一目标仓库内出现重复目标路径。
    """
    groups = {}
    for path in paths:
        item = parse_source_path(path)
        key = (item["org"].lower(), item["repo"].lower(), item["branch"])
        group = groups.setdefault(key, {
            "org": item["org"], "repo": item["repo"],
            "branch": item["branch"], "source_paths": [], "files": [],
            "_target_paths": set(),
        })
        target_path = item["target_path"]
        if target_path in group["_target_paths"]:
            raise ValueError(
                f"目标仓库 {item['org']}/{item['repo']} 内多个源路径映射到"
                f"同一目标路径: {target_path}")
        group["_target_paths"].add(target_path)
        group["source_paths"].append(item["source_path"])
        group["files"].append({
            "source_path": item["source_path"],
            "target_path": target_path,
        })

    result = []
    for group in groups.values():
        group.pop("_target_paths", None)
        result.append(group)
    return result
