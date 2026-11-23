"""目标仓路径解析与权威仓路径索引。

HTTP API 的 ``paths`` 是**目标仓坐标**：``组织/仓库/分支/文件相对路径``，
例如 ``Ascend/msagent/26.2.0/docs/en/agent_guide/accuracy.md`` 表示
「提交到 ``Ascend/msagent`` 仓的 ``26.2.0`` 分支，文件放在仓内
``docs/en/agent_guide/accuracy.md``」。调用方不需要知道权威仓的目录布局。

权威仓（``Computing_Ecosystem_Docs-EN``）里的存放形态与目标仓不同，且按
组织区分：

- Ascend：``Ascend/<产品>/<版本>/<仓库>/<相对路径>``
  （如 ``Ascend/MindStudio/26.2.0/msagent/docs/en/agent_guide/accuracy.md``）
- CANN 等：``<组织>/<版本>/<仓库>/<相对路径>``
- 少数仓库本身是顶层目录（如 ``Ascend/docs``）：``Ascend/docs/<子产品>/<版本>/<相对路径>``

``SourceIndex`` 在克隆权威仓之后按上述形态建索引，把目标坐标精确映射回
权威仓文件：唯一命中才用，找不到或命中多个一律拒绝——不做模糊匹配，也
不按「最像的那个」猜测。
"""

import re

from translator.index import (
    DEFAULT_REPO_DEPTH,
    REPO_DEPTH_BY_ORG,
    _target_rel_path,
)

# 分支/版本段特征：master、main、26.2.0、v3.2.0、branch_v26.1.0_更新、
# release_v26.2.0、core_r0.18.0、v2.7.1-26.1.0-en-update-xxxx 等。
_VERSION_RE = re.compile(
    r"^(?:master|main|(?:branch|release|core)_[^/]+|v?\d[\w.\-]*)$",
    re.IGNORECASE)


def _looks_like_version(segment):
    """判断路径段是否像分支/版本名（版本号、master/main、发布分支）。"""
    return bool(_VERSION_RE.match(segment or ""))


def parse_target_path(path):
    """解析单个目标仓坐标路径。

    Args:
        path: 形如 ``Ascend/msagent/26.2.0/docs/en/agent_guide/accuracy.md``
            的路径：组织/仓库/分支/文件相对路径。

    Returns:
        dict: source_path（原样）/ org / repo / branch / rel / target_path。

    Raises:
        ValueError: 空路径、绝对路径、含 ..、层级不足，或第 2/3 段不像
            仓库名/分支名时抛出。
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("路径不能为空")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"路径必须为仓内相对路径: {raw!r}")
    if "\\" in raw:
        raise ValueError(f"路径不能包含反斜杠: {raw!r}")

    segments = raw.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ValueError(f"路径包含空段、. 或 ..: {raw!r}")
    if len(segments) < 4:
        raise ValueError(
            f"路径必须形如 组织/仓库/分支/文件（当前只有 {len(segments)} 段）: {raw!r}")

    org, repo, branch = segments[0], segments[1], segments[2]
    rel = "/".join(segments[3:])

    # 第 2 段必须是仓库名：传了组织/版本/仓库（权威仓写法）时会在这里报错，
    # 避免被误当成「仓库名叫 9.2.0」而在后面失败，错误信息也更可读。
    if _looks_like_version(repo):
        raise ValueError(
            f"第 2 段应为仓库名而不是版本/分支名（{repo!r}）：paths 需为目标仓"
            f"坐标 组织/仓库/分支/文件，例如 Ascend/msagent/26.2.0/docs/en/a.md: {raw!r}")
    if not _looks_like_version(branch):
        raise ValueError(
            f"第 3 段应为分支/版本名（{branch!r}）：paths 需为目标仓坐标 "
            f"组织/仓库/分支/文件，例如 CANN/hccl/9.2.0/docs/en/a.md: {raw!r}")

    return {
        "source_path": raw,
        "org": org,
        "repo": repo,
        "branch": branch,
        "rel": rel,
        "target_path": _target_rel_path(org, repo, rel),
    }


def group_paths(paths):
    """按 (org, repo, branch) 分组，供「同仓同分支合并为一个 PR」。

    Args:
        paths: 目标仓坐标路径列表（调用方已去重）。

    Returns:
        list[dict]: [{org, repo, branch, source_paths,
                      files: [{source_path, rel, target_path}]}]

    Raises:
        ValueError: 任一格式非法，或同一目标仓库内出现重复目标路径。
    """
    groups = {}
    for path in paths:
        item = parse_target_path(path)
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
            "rel": item["rel"],
            "target_path": target_path,
        })

    result = []
    for group in groups.values():
        group.pop("_target_paths", None)
        result.append(group)
    return result


def _split_standard(segments):
    """按组织层级把权威仓路径切成 (org, repo, branch, rel)。

    与引擎 ``translator/index.py`` 的口径一致：Ascend 为 4 段
    （org/产品/版本/仓库），其余组织默认 3 段（org/版本/仓库）；未登记组织
    且第 2 段不像版本时按 4 段布局兜底。

    Returns:
        tuple | None: 切分失败（层级不足或无版本段）返回 None。
    """
    org = segments[0]
    depth = REPO_DEPTH_BY_ORG.get(org, DEFAULT_REPO_DEPTH)
    version_index = None
    for index in range(1, min(depth - 1, len(segments))):
        if _looks_like_version(segments[index]):
            version_index = index
    if version_index is None and depth == DEFAULT_REPO_DEPTH \
            and len(segments) > 3 and _looks_like_version(segments[2]) \
            and not _looks_like_version(segments[1]):
        # 未登记组织的 4 段布局：org/产品/版本/仓库
        depth, version_index = 4, 2
    if version_index is None or len(segments) <= depth:
        return None
    return (org, segments[depth - 1], segments[version_index],
            "/".join(segments[depth:]))


def _record(single, multi, key, path):
    """写入索引；同一 key 命中第二个文件时移入 multi（不覆盖、不猜）。"""
    if key in single:
        multi.setdefault(key, [single.pop(key)]).append(path)
    elif key in multi:
        multi[key].append(path)
    else:
        single[key] = path


class SourceIndex:
    """权威仓文件树索引：目标仓坐标 → 权威仓内文件路径（精确匹配）。

    两种形态各建一张表，按「该仓库在权威仓里是否顶层目录」二选一查：

    - ``_exact``：标准形态 ``org/<产品>/<版本>/<仓库>/<rel>`` 或
      ``org/<版本>/<仓库>/<rel>``；
    - ``_repo_sub``：仓库本身是顶层目录 ``org/<仓库>/[<子产品>/]<版本>/<rel>``
      （``Ascend/docs`` 这类）。

    命中多个（权威仓里同一条坐标对应多份文件）时视为无法确定，交给调用方
    报错，绝不挑一个提交。
    """

    def __init__(self, tree_paths=()):
        self._top_dirs = set()      # 小写的 "org/repo"
        self._exact = {}
        self._exact_multi = {}
        self._repo_sub = {}
        self._repo_sub_multi = {}
        for path in tree_paths:
            self._add(path)

    def _add(self, path):
        segments = path.split("/")
        if len(segments) < 3:
            return
        org, second = segments[0], segments[1]
        self._top_dirs.add(f"{org.lower()}/{second.lower()}")

        # 形态一：标准切分（产品层在版本之前，仓库在版本之后）
        split = _split_standard(segments)
        if split is not None:
            repo, branch, rel = split[1], split[2], split[3]
            _record(self._exact, self._exact_multi,
                    (org.lower(), repo.lower(), branch, rel), path)

        # 形态二：仓库即顶层目录，其后可带一层子产品再是版本
        tail = segments[2:]
        for index in (0, 1):
            if index + 1 < len(tail) and _looks_like_version(tail[index]):
                _record(self._repo_sub, self._repo_sub_multi,
                        (org.lower(), second.lower(), tail[index],
                         "/".join(tail[index + 1:])), path)
                break

    def has_repo_dir(self, org, repo):
        """该仓库在权威仓里是否作为顶层目录存在（决定用哪种形态查）。"""
        return f"{org.lower()}/{repo.lower()}" in self._top_dirs

    def candidates(self, org, repo, branch, rel):
        """返回该目标坐标命中的全部权威仓文件（0/1/多个）。"""
        key = (org.lower(), repo.lower(), branch, rel)
        if self.has_repo_dir(org, repo):
            single, multi = self._repo_sub, self._repo_sub_multi
        else:
            single, multi = self._exact, self._exact_multi
        if key in multi:
            return list(multi[key])
        path = single.get(key)
        return [path] if path else []

    def resolve(self, org, repo, branch, rel):
        """把目标仓坐标解析为权威仓文件路径；非唯一命中返回 None。"""
        hits = self.candidates(org, repo, branch, rel)
        return hits[0] if len(hits) == 1 else None


def build_source_index(tree_paths):
    """用一次克隆的文件树构建索引（O(n)，之后查表 O(1)）。"""
    return SourceIndex(tree_paths)
