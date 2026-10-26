"""webapp.paths：权威仓路径解析与分组。"""

import pytest

from webapp.paths import group_paths, parse_source_path


class TestParseSourcePath:
    """按组织层级解析 org/repo/branch/目标路径。"""

    @pytest.mark.parametrize("path, org, repo, branch, target", [
        # Ascend 当前布局：org/产品/版本/仓库
        ("Ascend/MindStudio/26.2.0/msprof/docs/en/guide.md",
         "Ascend", "msprof", "26.2.0", "docs/en/guide.md"),
        # Ascend 旧布局：org/版本/产品/仓库
        ("Ascend/26.1.0/MindStudio/msprof/docs/en/guide.md",
         "Ascend", "msprof", "26.1.0", "docs/en/guide.md"),
        # CANN 默认 3 段：org/版本/仓库
        ("CANN/9.1.0/hccl/docs/en/guide.md",
         "CANN", "hccl", "9.1.0", "docs/en/guide.md"),
        # 未登记组织的 4 段布局：org/产品/版本/仓库
        ("Kunpeng/BoostKit/1.2.0/kml/docs/a.md",
         "Kunpeng", "kml", "1.2.0", "docs/a.md"),
        # master/main 分支
        ("CANN/master/sip/docs/a.md",
         "CANN", "sip", "master", "docs/a.md"),
        # 发布分支写法
        ("CANN/release_v26.2.0/sip/docs/a.md",
         "CANN", "sip", "release_v26.2.0", "docs/a.md"),
        ("Ascend/MindStudio/branch_v26.1.0_更新/msprof/docs/a.md",
         "Ascend", "msprof", "branch_v26.1.0_更新", "docs/a.md"),
    ])
    def test_layouts(self, path, org, repo, branch, target):
        item = parse_source_path(path)
        assert (item["org"], item["repo"], item["branch"]) == \
            (org, repo, branch)
        assert item["target_path"] == target
        assert item["source_path"] == path

    def test_target_path_rules_applied(self):
        """CANN/docs 补 docs/ 前缀；CANN/release-management 拍平 en/。"""
        docs = parse_source_path("CANN/9.1.0/docs/en/guide.md")
        assert docs["target_path"] == "docs/en/guide.md"
        release = parse_source_path(
            "CANN/9.1.0/release-management/en/guide.md")
        assert release["target_path"] == "guide_en.md"
        plain = parse_source_path("CANN/9.1.0/release-management/guide.md")
        assert plain["target_path"] == "guide.md"

    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        "/Ascend/MindStudio/26.2.0/msprof/docs/a.md",
        "Ascend\\MindStudio\\26.2.0\\msprof\\docs\\a.md",
        "Ascend//26.2.0/msprof/docs/a.md",
        "Ascend/MindStudio/../msprof/docs/a.md",
        "CANN",                                  # 层级不足
        "CANN/9.1.0",                            # 缺仓库与文件
        "CANN/9.1.0/hccl",                       # 指向仓库目录
        "Ascend/MindStudio/26.2.0/msprof",       # 指向仓库目录
        "Ascend/MindStudio/msprof/docs/a.md",    # 无版本段
        "CANN/hccl/docs/a.md",                   # 无版本段
    ])
    def test_invalid_paths_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_source_path(bad)


class TestGroupPaths:
    """同 (org, repo, branch) 合并为一个 PR 分组。"""

    def test_groups_by_repo_and_branch(self):
        groups = group_paths([
            "CANN/9.1.0/hccl/docs/en/a.md",
            "CANN/9.1.0/hccl/docs/en/b.md",
            "CANN/9.1.0/sip/docs/en/a.md",
            "CANN/26.1.0/hccl/docs/en/a.md",
        ])
        keys = {(g["org"], g["repo"], g["branch"]): g for g in groups}
        assert set(keys) == {
            ("CANN", "hccl", "9.1.0"),
            ("CANN", "sip", "9.1.0"),
            ("CANN", "hccl", "26.1.0"),
        }
        hccl = keys[("CANN", "hccl", "9.1.0")]
        assert hccl["source_paths"] == [
            "CANN/9.1.0/hccl/docs/en/a.md",
            "CANN/9.1.0/hccl/docs/en/b.md",
        ]
        assert [f["target_path"] for f in hccl["files"]] == [
            "docs/en/a.md", "docs/en/b.md"]

    def test_org_repo_case_insensitive_merge(self):
        """AtomGit 名称大小写不敏感：同仓不同大小写合并为一组。"""
        groups = group_paths([
            "CANN/9.1.0/hccl/docs/a.md",
            "cann/9.1.0/HCCL/docs/b.md",
        ])
        assert len(groups) == 1
        assert groups[0]["org"] == "CANN"
        assert groups[0]["repo"] == "hccl"
        assert groups[0]["source_paths"] == [
            "CANN/9.1.0/hccl/docs/a.md",
            "cann/9.1.0/HCCL/docs/b.md",
        ]

    def test_duplicate_target_path_rejected(self):
        """同仓内多个来源映射到同一目标路径 → 拒绝（避免互相覆盖）。"""
        with pytest.raises(ValueError, match="同一目标路径"):
            group_paths([
                # en_flat 规则：en/a_en.md 拍平后与根目录 a_en.md 同名
                "CANN/9.1.0/release-management/en/a_en.md",
                "CANN/9.1.0/release-management/a_en.md",
            ])

    def test_invalid_path_propagates(self):
        with pytest.raises(ValueError):
            group_paths(["CANN/9.1.0/hccl"])  # 指向仓库目录
