"""目录层级与目标路径改写规则。

只保留 HTTP 服务需要的两项能力：
- REPO_DEPTH_BY_ORG / DEFAULT_REPO_DEPTH：按组织识别仓库级目录段数
- _target_rel_path：把仓库级目录下的相对路径改写为目标仓库内路径

历史 CLI 的扫描/同步逻辑（sync_source、_list_subdirs、_walk_files 等）
已随 Jenkins 入口一并移除。
"""

# 各产品组织在权威仓中的「仓库级目录」段数（org/.../仓库名）：
#   Ascend: org/产品/版本/仓库 → 4 段
#   CANN:   org/版本/仓库      → 3 段
# 未登记的组织按默认 3 段处理。
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
