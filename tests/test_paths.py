"""webapp.paths：目标仓坐标解析、分组与权威仓路径索引。"""

import pytest

from webapp.paths import build_source_index, group_paths, parse_target_path


class TestParseTargetPath:
    """paths 为目标仓坐标：组织/仓库/分支/文件相对路径。"""

    @pytest.mark.parametrize("path, org, repo, branch, rel", [
        ("Ascend/msagent/26.2.0/docs/en/agent_guide/accuracy.md",
         "Ascend", "msagent", "26.2.0", "docs/en/agent_guide/accuracy.md"),
        ("CANN/hccl/9.2.0/docs/en/a.md",
         "CANN", "hccl", "9.2.0", "docs/en/a.md"),
        ("CANN/sip/master/docs/a.md",
         "CANN", "sip", "master", "docs/a.md"),
        ("CANN/sip/release_v26.2.0/docs/a.md",
         "CANN", "sip", "release_v26.2.0", "docs/a.md"),
        ("Ascend/msagent/branch_v26.1.0_更新/docs/a.md",
         "Ascend", "msagent", "branch_v26.1.0_更新", "docs/a.md"),
        ("Kunpeng/kml/v3.2.0/docs/a.md",
         "Kunpeng", "kml", "v3.2.0", "docs/a.md"),
    ])
    def test_coordinates(self, path, org, repo, branch, rel):
        item = parse_target_path(path)
        assert (item["org"], item["repo"], item["branch"]) == \
            (org, repo, branch)
        assert item["rel"] == rel
        assert item["target_path"] == rel
        assert item["source_path"] == path

    def test_target_path_rules_applied(self):
        """CANN/docs 补 docs/ 前缀；CANN/release-management 拍平 en/。"""
        docs = parse_target_path("CANN/docs/9.1.0/en/guide.md")
        assert docs["target_path"] == "docs/en/guide.md"
        release = parse_target_path(
            "CANN/release-management/9.1.0/en/guide.md")
        assert release["target_path"] == "guide_en.md"
        plain = parse_target_path("CANN/release-management/9.1.0/guide.md")
        assert plain["target_path"] == "guide.md"

    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        "/CANN/hccl/9.1.0/docs/a.md",
        "CANN\\hccl\\9.1.0\\docs\\a.md",
        "CANN//9.1.0/docs/a.md",
        "CANN/hccl/../docs/a.md",
        "CANN",                                  # 层级不足
        "CANN/hccl",                             # 缺分支与文件
        "CANN/hccl/docs/a.md",                   # 缺分支
        "CANN/9.1.0/hccl/docs/a.md",             # 权威仓写法：第 2 段是版本
        "Ascend/msagent/docs/a.md",              # 缺分支
    ])
    def test_invalid_paths_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_target_path(bad)


class TestGroupPaths:
    """同 (org, repo, branch) 合并为一个 PR 分组。"""

    def test_groups_by_repo_and_branch(self):
        groups = group_paths([
            "CANN/hccl/9.1.0/docs/en/a.md",
            "CANN/hccl/9.1.0/docs/en/b.md",
            "CANN/sip/9.1.0/docs/en/a.md",
            "CANN/hccl/26.1.0/docs/en/a.md",
        ])
        keys = {(g["org"], g["repo"], g["branch"]): g for g in groups}
        assert set(keys) == {
            ("CANN", "hccl", "9.1.0"),
            ("CANN", "sip", "9.1.0"),
            ("CANN", "hccl", "26.1.0"),
        }
        hccl = keys[("CANN", "hccl", "9.1.0")]
        assert hccl["source_paths"] == [
            "CANN/hccl/9.1.0/docs/en/a.md",
            "CANN/hccl/9.1.0/docs/en/b.md",
        ]
        assert [f["rel"] for f in hccl["files"]] == [
            "docs/en/a.md", "docs/en/b.md"]
        assert [f["target_path"] for f in hccl["files"]] == [
            "docs/en/a.md", "docs/en/b.md"]

    def test_org_repo_case_insensitive_merge(self):
        """AtomGit 名称大小写不敏感：同仓不同大小写合并为一组。"""
        groups = group_paths([
            "CANN/hccl/9.1.0/docs/a.md",
            "cann/HCCL/9.1.0/docs/b.md",
        ])
        assert len(groups) == 1
        assert groups[0]["org"] == "CANN"
        assert groups[0]["repo"] == "hccl"
        assert groups[0]["source_paths"] == [
            "CANN/hccl/9.1.0/docs/a.md",
            "cann/HCCL/9.1.0/docs/b.md",
        ]

    def test_duplicate_target_path_rejected(self):
        """同仓内多个来源映射到同一目标路径 → 拒绝（避免互相覆盖）。"""
        with pytest.raises(ValueError, match="同一目标路径"):
            group_paths([
                # en_flat 规则：en/a_en.md 拍平后与根目录 a_en.md 同名
                "CANN/release-management/9.1.0/en/a_en.md",
                "CANN/release-management/9.1.0/a_en.md",
            ])

    def test_invalid_path_propagates(self):
        with pytest.raises(ValueError):
            group_paths(["CANN/hccl"])  # 层级不足


# 权威仓真实布局的切片：Ascend 带产品层、CANN 不带、docs 仓是顶层目录
TREE = [
    "Ascend/MindStudio/26.2.0/msagent/docs/en/agent_guide/accuracy.md",
    "Ascend/MindStudio/master/msagent/docs/en/agent_guide/accuracy.md",
    "Ascend/docs/MindStudio/26.2.0/en/menu/guide.md",
    "Ascend/FSDPTurbo/26.2.0/docs/en/SECURITYNOTE.md",
    "CANN/9.2.0/hccl/en/docs/api_ref/README.md",
    "CANN/9.2.0/sip/docs/en/API_Reference/BLAS/Asum.md",
]


class TestSourceIndex:
    """目标仓坐标 → 权威仓路径：精确匹配，不做模糊猜测。"""

    @pytest.fixture
    def index(self):
        return build_source_index(TREE)

    def test_ascend_product_layer_filled(self, index):
        """Ascend/<产品>/<版本>/<仓库>/... 的产品层由索引补齐。"""
        assert index.resolve(
            "Ascend", "msagent", "26.2.0",
            "docs/en/agent_guide/accuracy.md") == (
            "Ascend/MindStudio/26.2.0/msagent/docs/en/agent_guide/accuracy.md")

    def test_branch_master(self, index):
        assert index.resolve(
            "Ascend", "msagent", "master",
            "docs/en/agent_guide/accuracy.md") == (
            "Ascend/MindStudio/master/msagent/docs/en/agent_guide/accuracy.md")

    def test_cann_layout(self, index):
        assert index.resolve(
            "CANN", "hccl", "9.2.0", "en/docs/api_ref/README.md") == (
            "CANN/9.2.0/hccl/en/docs/api_ref/README.md")
        assert index.resolve(
            "CANN", "sip", "9.2.0",
            "docs/en/API_Reference/BLAS/Asum.md") == (
            "CANN/9.2.0/sip/docs/en/API_Reference/BLAS/Asum.md")

    def test_repo_as_top_dir(self, index):
        """Ascend/docs 的镜像在 Ascend/docs/<子产品>/<版本>/ 下。"""
        assert index.resolve(
            "Ascend", "docs", "26.2.0", "en/menu/guide.md") == (
            "Ascend/docs/MindStudio/26.2.0/en/menu/guide.md")

    def test_repo_as_top_dir_not_confused_with_other_products(self, index):
        """其它产品的 docs 目录不属于 Ascend/docs：必须判失败而不是乱选。"""
        assert index.candidates(
            "Ascend", "docs", "26.2.0", "en/SECURITYNOTE.md") == []

    def test_missing_file(self, index):
        assert index.resolve(
            "Ascend", "msagent", "26.2.0", "docs/en/nope.md") is None

    def test_unknown_repo_not_fallback_to_partial(self, index):
        assert index.resolve("CANN", "runtime", "9.2.0", "docs/a.md") is None

    def test_case_insensitive_org_repo(self, index):
        assert index.resolve(
            "ascend", "MSAGENT", "26.2.0",
            "docs/en/agent_guide/accuracy.md") == (
            "Ascend/MindStudio/26.2.0/msagent/docs/en/agent_guide/accuracy.md")

    def test_ambiguous_is_not_guessed(self):
        """同一坐标命中多份（如两个产品下同名仓）→ 不解析，交上层报错。"""
        index = build_source_index([
            "Ascend/alpha/26.2.0/kml/docs/a.md",
            "Ascend/beta/26.2.0/kml/docs/a.md",
        ])
        assert index.resolve("Ascend", "kml", "26.2.0", "docs/a.md") is None
        assert len(index.candidates("Ascend", "kml", "26.2.0", "docs/a.md")) == 2
