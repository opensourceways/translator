"""service 模块的单元测试。

覆盖幂等提交（submit_repo）、分支重建（rebuild_branch）与 client 重试。
测试通过 mock 各子模块的 client 完全控制下游 AtomGit API 依赖，
不发起真实网络请求。
"""

import base64
import os
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
import requests

from translator import client, common, index, localgit, submit
from translator.client import AtomGitError

SILENT = lambda msg: None  # 测试中静默日志


def _entry(name, path, entry_type):
    """构造目录条目。"""
    return {"name": name, "path": path, "type": entry_type}


# 权威仓库的模拟目录树（按组织层级：Ascend=组织/版本/产品/仓库，CANN=组织/版本/仓库）
FAKE_TREE = {
    "": [
        _entry("Ascend", "Ascend", "dir"),
        _entry("CANN", "CANN", "dir"),
        _entry("README.md", "README.md", "file"),
    ],
    "Ascend": [
        _entry("26.1.0", "Ascend/26.1.0", "dir"),
        _entry("master", "Ascend/master", "dir"),
    ],
    "Ascend/26.1.0": [_entry("MindStudio", "Ascend/26.1.0/MindStudio", "dir")],
    "Ascend/26.1.0/MindStudio": [
        _entry("msprof", "Ascend/26.1.0/MindStudio/msprof", "dir"),
        _entry("mspti", "Ascend/26.1.0/MindStudio/mspti", "dir"),
    ],
    "Ascend/26.1.0/MindStudio/msprof": [
        _entry("test", "Ascend/26.1.0/MindStudio/msprof/test", "file"),
    ],
    "Ascend/26.1.0/MindStudio/mspti": [
        _entry("test", "Ascend/26.1.0/MindStudio/mspti/test", "file"),
    ],
    "Ascend/master": [_entry("MindStudio", "Ascend/master/MindStudio", "dir")],
    "Ascend/master/MindStudio": [
        _entry("msprof", "Ascend/master/MindStudio/msprof", "dir"),
    ],
    "Ascend/master/MindStudio/msprof": [
        _entry("test", "Ascend/master/MindStudio/msprof/test", "file"),
    ],
    "CANN": [_entry("9.1.0", "CANN/9.1.0", "dir"),
             _entry("master", "CANN/master", "dir")],
    "CANN/9.1.0": [_entry("cann", "CANN/9.1.0/cann", "dir")],
    "CANN/9.1.0/cann": [
        _entry("README.md", "CANN/9.1.0/cann/README.md", "file"),
    ],
    "CANN/master": [
        _entry("release-management", "CANN/master/release-management", "dir"),
    ],
    "CANN/master/release-management": [
        _entry("en", "CANN/master/release-management/en", "dir"),
    ],
    "CANN/master/release-management/en": [
        _entry("9.1.1", "CANN/master/release-management/en/9.1.1", "dir"),
    ],
    "CANN/master/release-management/en/9.1.1": [
        _entry("release-notes_en.md",
               "CANN/master/release-management/en/9.1.1/release-notes_en.md",
               "file"),
    ],
}

# 模拟树中的仓库级目录（共 5 个）
REPO_PATHS = [
    "Ascend/26.1.0/MindStudio/msprof",
    "Ascend/26.1.0/MindStudio/mspti",
    "Ascend/master/MindStudio/msprof",
    "CANN/9.1.0/cann",
    "CANN/master/release-management",
]


def _make_client_mock():
    """构造 mock 的 client：get_repo 返回默认分支，list_dir 按模拟树返回。"""
    client_mock = MagicMock()
    client_mock.AtomGitError = AtomGitError
    client_mock.get_repo.return_value = {"default_branch": "master"}
    client_mock.list_dir.side_effect = (
        lambda owner, repo, path, ref, token: FAKE_TREE[path]
    )
    return client_mock


def _make_scan_client_mock():
    """构造扫描用的 mock 客户端：源仓下载固定内容，目标分支默认存在。"""
    client_mock = _make_client_mock()
    client_mock.get_file_bytes.return_value = b"content"
    client_mock.list_branches.return_value = [
        {"name": "26.1.0"}, {"name": "master"}, {"name": "9.1.0"},
    ]
    client_mock.get_contents.return_value = None
    return client_mock


def _tree_with_file_shas(sha):
    """返回文件条目带 blob sha 的目录树副本（模拟目录条目携带 sha）。

    sha 取与内容无关的固定值，测试仅关注「来源条目 sha 是否被用于
    免下载比对」，不关心其与 blob sha 的一致性。
    """
    import copy
    tree = copy.deepcopy(FAKE_TREE)
    for entries in tree.values():
        for e in entries:
            if e["type"] == "file":
                e["sha"] = sha
    return tree


def _make_sha_scan_client_mock(entry_sha, target_sha):
    """构造「目录条目带 sha」的扫描 mock：来源 sha 与目标 sha 可分别指定。"""
    client_mock = _make_scan_client_mock()
    client_mock.list_dir.side_effect = (
        lambda owner, repo, path, ref, token: _tree_with_file_shas(entry_sha)[path]
    )
    client_mock.get_contents.return_value = {"sha": target_sha}
    return client_mock


class TestSourceRepoOverride:
    """来源仓库可用 ATOMGIT_SOURCE_REPO 覆盖为自己的 Fork。"""

    def test_default_is_authoritative(self, monkeypatch):
        monkeypatch.delenv("ATOMGIT_SOURCE_REPO", raising=False)
        assert common._source_repo() == (
            common.SOURCE_ORG, common.SOURCE_REPO)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATOMGIT_SOURCE_REPO", "user/Fork-Repo")
        assert common._source_repo() == ("user", "Fork-Repo")

    def test_env_full_url_tolerated(self, monkeypatch):
        """粘贴完整 URL 或带 .git 后缀时也能正确解析。"""
        for value in (
            "https://atomgit.com/user/Fork-Repo",
            "https://atomgit.com/user/Fork-Repo.git",
            "user/Fork-Repo.git",
            "user/Fork-Repo/",
        ):
            monkeypatch.setenv("ATOMGIT_SOURCE_REPO", value)
            assert common._source_repo() == ("user", "Fork-Repo")


def _make_files():
    """构造一个文件的变更清单（内容为 b"content"）。"""
    return [{
        "target_path": "docs/en/test",
        "content_b64": base64.b64encode(b"content").decode("ascii"),
    }]


def _make_submit_client_mock():
    """构造走通整个 submit_repo 流程的 mock 客户端。

    已有 Fork；目标分支 26.1.0 存在（sha abc123）；无已开 PR。
    """
    client_mock = MagicMock()
    client_mock.AtomGitError = AtomGitError
    client_mock.get_user.return_value = {"login": "bot",
                                         "email": "bot@huawei.com"}
    client_mock.list_repo_forks.return_value = [
        {"full_name": "bot/MindStudio-Profiler", "owner": {"login": "bot"}},
    ]

    def list_branches(owner, repo, token):
        if owner == "Ascend":
            return [{"name": "26.1.0", "commit": {"sha": "abc123"}}]
        return []  # Fork 上的分支

    client_mock.list_branches.side_effect = list_branches
    client_mock.sync_repo.return_value = {}
    client_mock.get_sync_status.return_value = {"repo_sync_result": True}
    client_mock.list_pull_requests.return_value = []
    client_mock.create_pr.return_value = {"number": 1, "html_url": "http://x/1"}
    return client_mock


def _make_localgit_mock(pushed=True, written=None):
    """构造 localgit mock：rebuild_branch 默认推送单 commit 并写入一个文件。"""
    localgit_mock = MagicMock()
    localgit_mock.rebuild_branch.return_value = {
        "pushed": pushed,
        "written": (written if written is not None
                    else [{"path": "docs/en/test", "action": "create"}]),
        "reason": "重建完成" if pushed else "远端已是最新单 commit",
    }
    return localgit_mock


class TestSubmitRepo:
    """幂等提交：单 commit 重建、内容未变跳过、open PR 去重。"""

    def test_success_rebuilds_branch_and_creates_pr(self):
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock()
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is False
            assert result["pr_url"] == "http://x/1"
            # 重建固定分支：以目标分支为基点，files 原样传入
            rb_args = localgit_mock.rebuild_branch.call_args.args
            assert rb_args[0] == "bot"  # Fork owner
            assert rb_args[1] == "MindStudio-Profiler"  # Fork 仓库名
            assert rb_args[3] == "26.1.0"  # 基点 = 目标分支
            assert rb_args[4] == "translator/auto-26.1.0"  # 固定分支
            assert rb_args[5] == _make_files()
            # commit 作者邮箱 = 账号绑定邮箱（CLA 按 commit 作者邮箱校验）
            assert localgit_mock.rebuild_branch.call_args.kwargs.get(
                "author_email") == "bot@huawei.com"
            # 跨仓 PR 带 fork_path，head 为 用户:固定分支
            pr_kwargs = client_mock.create_pr.call_args
            assert pr_kwargs.args[3] == "bot:translator/auto-26.1.0"
            assert pr_kwargs.args[4] == "26.1.0"
            assert pr_kwargs.kwargs["fork_path"] == "bot/MindStudio-Profiler"
            assert "translator" in pr_kwargs.args[5]

    def test_legacy_history_rewrite_reported_as_push(self):
        """存量碎 commit 分支的历史压缩：rebuild 返回 pushed=True、written 空，
        且已有 open PR 时只并入既有 PR（PR diff 自动变为单 commit）。"""
        client_mock = _make_submit_client_mock()
        # 重建只压缩了历史（written 为空）但已强推
        localgit_mock = _make_localgit_mock(pushed=True, written=[])
        client_mock.list_pull_requests.return_value = [{
            "number": 42, "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_pr.assert_not_called()

    def test_legacy_history_rewrite_without_pr_returns_branch_url(self):
        """历史压缩后无已开 PR：不创建空 PR，回传目标分支链接。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock(pushed=True, written=[])
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["content_exists"] is True
            assert result["pr_url"].endswith("/tree/26.1.0")
            client_mock.create_pr.assert_not_called()

    def test_noop_with_merged_history_pr_returns_it(self):
        """noop 且存在已合并的历史 PR：回传该 PR，供调用方判定已上线。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.side_effect = [
            [],  # 当前没有未合并 PR
            [{  # state=all 时回捞到的历史 PR
                "number": 7,
                "html_url": "http://x/7",
                "merged": True,
                "head": {"label": "translator/auto-26.1.0",
                         "ref": "translator/auto-26.1.0"},
                "base": {"ref": "26.1.0"},
            }],
        ]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit",
                      _make_localgit_mock(pushed=False, written=[])):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result.get("noop") is not True
            assert result["pr_existed"] is True
            assert result["pr_url"] == "http://x/7"
            assert result["pr_number"] == 7
            client_mock.create_pr.assert_not_called()

    def test_noop_without_any_pr_returns_branch_url(self):
        """noop 且历史里也没有该分支的 PR：返回目标分支链接（内容已在分支）。

        调用方拿这个链接查 /prs/status 会得到 merged，任务得以推进到已上线。
        """
        client_mock = _make_submit_client_mock()  # list_pull_requests 恒为空
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit",
                      _make_localgit_mock(pushed=False, written=[])):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result.get("noop") is not True
            assert result["content_exists"] is True
            assert result["pr_url"] == (
                "https://atomgit.com/Ascend/msprof/tree/26.1.0")
            assert result["pr_number"] is None
            client_mock.create_pr.assert_not_called()

    def test_pr_title_body_groups_create_update(self):
        """PR 标题/描述按新增/更新分组。"""
        title, body = submit._make_pr_title_body([
            {"path": "a.md", "action": "create"},
            {"path": "b.md", "action": "update"},
        ])
        assert title == "新增 1 份，更新 1 份 英文版文档"
        assert "## 新增" in body and "## 更新" in body
        assert "`a.md`" in body and "`b.md`" in body

        title, _ = submit._make_pr_title_body(
            [{"path": "b.md", "action": "update"}])
        assert title == "更新 b.md 英文版文档"

    def test_identical_content_returns_branch_url(self):
        """内容与目标分支一致（rebuild 未推送）且无 PR：回传分支链接。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock(pushed=False, written=[])
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["content_exists"] is True
            assert result["pr_url"] == (
                "https://atomgit.com/Ascend/msprof/tree/26.1.0")
            client_mock.create_pr.assert_not_called()

    def test_open_pr_dedup(self):
        """已存在同一 head/base 的 open PR 时不重复创建，返回已有 PR。"""
        client_mock = _make_submit_client_mock()
        # 真实 API 的 head.label 只有分支名，不含用户前缀
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "translator/auto-26.1.0",
                     "ref": "translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            assert result["pr_url"] == "http://x/42"
            assert result["pr_number"] == 42
            client_mock.create_pr.assert_not_called()
            # 服务端不支持 head/base 过滤，拉全量 open PR 后客户端过滤
            _, kwargs = client_mock.list_pull_requests.call_args
            assert "head" not in kwargs
            assert "base" not in kwargs

    def test_pr_other_branch_not_deduped(self):
        """head 分支不同的 open PR 不影响本仓库创建新 PR。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-master"},
            "base": {"ref": "master"},
        }]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["pr_existed"] is False
            client_mock.create_pr.assert_called_once()

    def test_identical_content_with_open_pr_is_noop(self):
        """内容未变（rebuild 未推送）且已有 open PR：不建 PR，纯幂等空转。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_pull_requests.return_value = [{
            "number": 42,
            "html_url": "http://x/42",
            "head": {"label": "bot:translator/auto-26.1.0"},
            "base": {"ref": "26.1.0"},
        }]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit",
                      _make_localgit_mock(pushed=False, written=[])):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            client_mock.create_pr.assert_not_called()

    def test_create_pr_409_treated_as_existing(self):
        """去重漏判时服务端返回 409：从错误消息提取 PR 号按已存在处理。"""
        client_mock = _make_submit_client_mock()
        client_mock.create_pr.side_effect = AtomGitError(
            409, '{"error_code":409,"error_message":"Another open merge '
                 'request already exists for this source branch: !443"}')
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert result["pr_existed"] is True
            assert result["pr_number"] == 443
            assert result["pr_url"].endswith("/merge_requests/443")

    def test_branch_not_exists(self):
        """目标仓库不存在版本分支时失败（不自动创建分支）。"""
        client_mock = _make_submit_client_mock()
        client_mock.list_branches.side_effect = (
            lambda owner, repo, token: [{"name": "master"}]
        )
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "check_branch"

    def test_rebuild_failure_no_pr(self):
        """分支重建失败（如 Fork 缺目标分支/推送失败）时不创建 PR。"""
        client_mock = _make_submit_client_mock()
        localgit_mock = _make_localgit_mock()
        localgit_mock.rebuild_branch.side_effect = AtomGitError(
            200, "git checkout -B 失败: 分支不存在")
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", localgit_mock):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "write"
            client_mock.create_pr.assert_not_called()

    def test_get_user_failure(self):
        client_mock = _make_submit_client_mock()
        client_mock.get_user.side_effect = AtomGitError(401, "Unauthorized")
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is False
            assert result["stage"] == "get_user"

    def test_sync_400_falls_back_to_full_sync(self):
        """按分支同步 400（Fork 缺少该分支）时退化为全量同步。"""
        client_mock = _make_submit_client_mock()
        client_mock.sync_repo.side_effect = [
            AtomGitError(400, "26.1.0 branch or tag not found"),
            {"repo_sync_result": False},
        ]
        # 全量同步后目标分支立即出现在 Fork 上（免于轮询等待）
        client_mock.list_branches.side_effect = lambda owner, repo, token: [
            {"name": "26.1.0", "commit": {"sha": "abc123"}}]
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()):
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            assert client_mock.sync_repo.call_count == 2
            second = client_mock.sync_repo.call_args_list[1]
            assert second.kwargs == {}

    def test_full_sync_waits_for_branch_to_appear(self):
        """全量同步是异步的：轮询 Fork 分支列表，等目标分支出现再重建。"""
        client_mock = _make_submit_client_mock()
        client_mock.sync_repo.side_effect = [
            AtomGitError(400, "26.1.0 branch or tag not found"),
            {},
        ]
        calls = {"n": 0}

        def list_branches(owner, repo, token):
            if owner == "Ascend":
                return [{"name": "26.1.0", "commit": {"sha": "abc123"}}]
            # Fork：前两次轮询分支尚未同步过来，第三次出现
            calls["n"] += 1
            return [{"name": "26.1.0"}] if calls["n"] >= 3 else []

        client_mock.list_branches.side_effect = list_branches
        with patch("translator.submit.client", client_mock), \
                patch("translator.submit.localgit", _make_localgit_mock()), \
                patch("translator.submit.time.sleep") as sleep_mock:
            result = submit.submit_repo(
                "Ascend", "msprof", "26.1.0", _make_files(), "tok", log=SILENT)
            assert result["ok"] is True
            # 等到了分支才继续（睡了 2 次），且没再查同步状态接口
            assert sleep_mock.call_count == 2
            client_mock.get_sync_status.assert_not_called()


class TestRebuildBranch:
    """rebuild_branch 真实 git 离线测试：本地 bare 仓库模拟 Fork，不触网。

    覆盖：首建单 commit、幂等跳过、更新强推、存量碎 commit 强制压缩、
    二进制字节精确提交。
    """

    HEAD = "translator/auto-master"

    def _git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                        capture_output=True)

    @pytest.fixture()
    def fork_bare(self, tmp_path):
        """创建 bare「Fork」：master 分支带一个基础文件。"""
        seed = tmp_path / "seed"
        seed.mkdir()
        self._git("init", "-b", "master", cwd=str(seed))
        (seed / "README.md").write_bytes(b"hello\n")
        self._git("add", "-A", cwd=str(seed))
        self._git("-C", str(seed), "-c", "user.name=t", "-c",
                  "user.email=t@t", "commit", "-m", "base init")
        bare = tmp_path / "fork.git"
        self._git("clone", "--bare", str(seed), str(bare))
        return bare

    def _pr_commit_count(self, bare):
        """PR 范围（master..固定分支）的 commit 数，即 PR 实际包含的 commit 数。"""
        out = subprocess.run(
            ["git", "-C", str(bare), "rev-list", "--count",
             f"master..{self.HEAD}"], capture_output=True, text=True)
        return int(out.stdout.strip())

    def _rebuild(self, bare, spec, author_email="u@huawei.com"):
        """对本地 bare 仓库执行 rebuild_branch，返回 (结果, 克隆目录)。"""
        dest = os.path.join(tempfile.gettempdir(),
                            f"rebuild-test-{id(spec)}-{os.getpid()}")
        with patch.object(localgit, "_repo_url", lambda *a: str(bare)):
            result = localgit.rebuild_branch(
                "u", "r", "tok", "master", self.HEAD,
                [{"target_path": p,
                  "content_b64": base64.b64encode(c).decode()}
                 for p, c in spec.items()],
                dest, log=SILENT, author_email=author_email)
        return result, dest

    def _cleanup(self, dest):
        shutil.rmtree(dest, ignore_errors=True)

    def test_first_rebuild_single_commit(self, fork_bare):
        """首次重建：全部写入合并为单个 commit。"""
        spec = {"a.md": b"A", "d/b.md": b"B"}
        result, dest = self._rebuild(fork_bare, spec)
        try:
            assert result["pushed"] is True
            acts = {w["path"]: w["action"] for w in result["written"]}
            assert acts == {"a.md": "create", "d/b.md": "create"}
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_unchanged_rerun_skips_push(self, fork_bare):
        """重复运行内容不变：不推送（远端已是本工具单 commit）。"""
        spec = {"a.md": b"A"}
        _, dest = self._rebuild(fork_bare, spec)
        self._cleanup(dest)
        result, dest = self._rebuild(fork_bare, spec)
        try:
            assert result["pushed"] is False
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_update_still_single_commit(self, fork_bare):
        """译稿更新：强推后 PR 范围仍单 commit，动作相对目标分支基点。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        result, dest = self._rebuild(fork_bare, {"a.md": b"A2"})
        try:
            assert result["pushed"] is True
            # 基点（master）上没有 a.md → PR 语义为新增
            acts = {w["path"]: w["action"] for w in result["written"]}
            assert acts == {"a.md": "create"}
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_legacy_fragmented_history_compacted(self, fork_bare):
        """存量碎 commit 分支：树相同时仍强推，把 PR 历史压缩为单 commit。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        # 手工在固定分支上追加 5 个碎 commit（模拟旧实现逐文件写入）
        work = tempfile.mkdtemp(prefix="legacy-")
        try:
            self._git("clone", str(fork_bare), work)
            self._git("checkout", self.HEAD, cwd=work)
            for i in range(5):
                with open(os.path.join(work, "a.md"), "ab") as fh:
                    fh.write(b"x")
                self._git("add", "-A", cwd=work)
                self._git("-C", work, "-c", "user.name=t", "-c",
                          "user.email=t@t", "commit",
                          "-m", f"新增 f{i}.md 英文版文档")
            self._git("push", cwd=work)
            assert self._pr_commit_count(fork_bare) == 6
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # 重建回正确内容：树与碎 commit 版不同 → 强推压缩
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)
        # 再次运行：树相同且已是本工具单 commit → 幂等跳过
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is False
        finally:
            self._cleanup(dest)

    def test_legacy_same_tree_still_compacted(self, fork_bare):
        """树与期望一致但历史是旧碎 commit：仍强推压缩（空 commit 重建历史）。"""
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        self._cleanup(dest)
        # 手工追加碎 commit 后把内容改回期望值：树相同、历史多 commit
        work = tempfile.mkdtemp(prefix="legacy2-")
        try:
            self._git("clone", str(fork_bare), work)
            self._git("checkout", self.HEAD, cwd=work)
            for i in range(3):
                with open(os.path.join(work, f"f{i}.md"), "wb") as fh:
                    fh.write(b"x")
                self._git("add", "-A", cwd=work)
                self._git("-C", work, "-c", "user.name=t", "-c",
                          "user.email=t@t", "commit",
                          "-m", f"新增 f{i}.md 英文版文档")
            # 撤销碎文件：树回到与期望一致，但 HEAD 是旧格式 message
            for i in range(3):
                os.remove(os.path.join(work, f"f{i}.md"))
            self._git("add", "-A", cwd=work)
            self._git("-C", work, "-c", "user.name=t", "-c",
                      "user.email=t@t", "commit", "-m", "新增 back.md 英文版文档")
            self._git("push", cwd=work)
            assert self._pr_commit_count(fork_bare) == 5
        finally:
            shutil.rmtree(work, ignore_errors=True)
        # 树与远端相同但历史非本工具单 commit → 仍强推压缩
        #（written 相对目标分支基点，故 a.md 仍计为新增）
        result, dest = self._rebuild(fork_bare, {"a.md": b"A"})
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
        finally:
            self._cleanup(dest)

    def test_commit_without_global_identity(self, fork_bare, monkeypatch):
        """无全局 git 身份（如 Jenkins agent）时仍能 commit（仓库级配置兜底），
        作者邮箱优先用账号绑定邮箱（CLA 校验依据），缺省退化为 noreply。"""
        fake_home = tempfile.mkdtemp(prefix="no-identity-")
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL",
                           os.path.join(fake_home, "gitconfig-empty"))
        try:
            # 显式传入账号绑定邮箱
            with patch.object(localgit, "_repo_url", lambda *a: str(fork_bare)):
                result = localgit.rebuild_branch(
                    "u", "r", "tok", "master", self.HEAD,
                    [{"target_path": "a.md",
                      "content_b64": base64.b64encode(b"A").decode()}],
                    os.path.join(tempfile.gettempdir(),
                                 f"rebuild-id-{os.getpid()}"),
                    log=SILENT, author_email="u@huawei.com")
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1",
                 "--format=%an %ae", self.HEAD],
                capture_output=True, text=True)
            assert out.stdout.strip() == "u u@huawei.com"
            self._cleanup(os.path.join(tempfile.gettempdir(),
                                       f"rebuild-id-{os.getpid()}"))
            # 未传邮箱：退化为 noreply 地址（换内容强制重建出新 commit）
            result, dest = self._rebuild(fork_bare, {"a.md": b"A2"},
                                         author_email=None)
            self._cleanup(dest)
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1",
                 "--format=%ae", self.HEAD],
                capture_output=True, text=True)
            assert out.stdout.strip() == "u@noreply.atomgit.com"
        finally:
            shutil.rmtree(fake_home, ignore_errors=True)

    def test_wrong_author_email_still_rewritten(self, fork_bare):
        """树相同但远端 commit 作者邮箱不对（如旧版 noreply 导致 CLA
        不通过）：即使内容无变化也强推重写，刷新作者邮箱。"""
        spec = {"a.md": b"A"}
        # 先用 noreply 邮箱建远端（模拟修复前的错误状态）
        result, dest = self._rebuild(fork_bare, spec, author_email=None)
        self._cleanup(dest)
        out = subprocess.run(
            ["git", "-C", str(fork_bare), "log", "-1", "--format=%ae",
             self.HEAD], capture_output=True, text=True)
        assert out.stdout.strip() == "u@noreply.atomgit.com"
        # 同内容、传正确邮箱：树相同但作者不对 → 强推重写
        result, dest = self._rebuild(fork_bare, spec,
                                     author_email="u@huawei.com")
        try:
            assert result["pushed"] is True
            assert self._pr_commit_count(fork_bare) == 1
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "log", "-1", "--format=%ae",
                 self.HEAD], capture_output=True, text=True)
            assert out.stdout.strip() == "u@huawei.com"
        finally:
            self._cleanup(dest)
        # 作者已正确后再跑：幂等跳过
        result, dest = self._rebuild(fork_bare, spec,
                                     author_email="u@huawei.com")
        try:
            assert result["pushed"] is False
        finally:
            self._cleanup(dest)

    def test_binary_bytes_exact(self, fork_bare):
        """二进制/CRLF 文件按原始字节提交，不被行尾规范化改写。"""
        gif = b"GIF89a\x01\x00\xf7\r\n"
        result, dest = self._rebuild(fork_bare, {"a.md": b"A", "g.gif": gif})
        try:
            assert result["pushed"] is True
            out = subprocess.run(
                ["git", "-C", str(fork_bare), "show", f"{self.HEAD}:g.gif"],
                capture_output=True)
            assert out.stdout == gif
        finally:
            self._cleanup(dest)


class TestListTree:
    """list_tree 用 contents API 逐层遍历（git trees recursive 有静默截断问题）。"""

    def test_walks_contents_recursively(self):
        """从根目录出发递归收集全部文件路径，目录不入结果。"""
        from translator import client as client_mod

        dirs = {
            "": [
                {"type": "dir", "path": "en"},
                {"type": "file", "path": "README.md"},
            ],
            "en": [
                {"type": "dir", "path": "en/9.1.0"},
                {"type": "file", "path": "en/release-notes.md"},
            ],
            "en/9.1.0": [
                {"type": "file", "path": "en/9.1.0/release-notes.md"},
            ],
        }
        with patch.object(client_mod, "list_dir",
                          side_effect=lambda o, r, p, ref, t: dirs[p]):
            paths = client_mod.list_tree(
                "cann", "release-management", "translator/auto-master", "t")
        assert paths == {
            "README.md", "en/release-notes.md", "en/9.1.0/release-notes.md",
        }

    def test_walk_failure_propagates(self):
        """任一目录查询失败即抛错，由调用方跳过清理（宁可不删，不可误删）。"""
        from translator import client as client_mod

        def list_dir(owner, repo, path, ref, token):
            if path == "en":
                raise AtomGitError(500, "boom")
            return [{"type": "dir", "path": "en"}]

        with patch.object(client_mod, "list_dir", side_effect=list_dir):
            with pytest.raises(AtomGitError):
                client_mod.list_tree("cann", "release-management", "master", "t")


class TestTargetRelPath:
    """仓库级目录下相对路径 → 目标仓库内路径的改写规则。"""

    def test_unregistered_repo_passthrough(self):
        """未登记的仓库直连：相对路径原样作为目标路径（sip/hccl）。"""
        assert index._target_rel_path(
            "CANN", "sip", "docs/en/context/infra-faqs.md"
        ) == "docs/en/context/infra-faqs.md"
        assert index._target_rel_path(
            "CANN", "hccl", "docs/en/api_ref/README.md"
        ) == "docs/en/api_ref/README.md"
        assert index._target_rel_path(
            "Ascend", "msprof", "docs/en/a.md") == "docs/en/a.md"

    def test_cann_docs_prepend_docs_prefix(self):
        """CANN/docs 仓内容根为 docs/：目标路径补 docs/ 前缀。"""
        assert index._target_rel_path(
            "CANN", "docs",
            "en/fusion-patterns/Graph_Fusion_Patterns/AABiasaddConvFusion.md"
        ) == "docs/en/fusion-patterns/Graph_Fusion_Patterns/" \
           "AABiasaddConvFusion.md"

    def test_release_management_flattens_en_dir(self):
        """release-management：剥离 en/，扩展名前补 _en（未带时）。"""
        assert index._target_rel_path(
            "CANN", "release-management",
            "en/9.1.0/release-notes.md"
        ) == "9.1.0/release-notes_en.md"

    def test_release_management_no_double_suffix(self):
        """release-management：文件名已带 _en 时不重复追加。"""
        assert index._target_rel_path(
            "CANN", "release-management",
            "en/9.1.1/release-notes_en.md"
        ) == "9.1.1/release-notes_en.md"

    def test_release_management_keeps_multi_dot_name(self):
        """release-management：多级扩展名只在最后一个扩展名前补 _en。"""
        assert index._target_rel_path(
            "CANN", "release-management", "en/9.1.0/a.b.md"
        ) == "9.1.0/a.b_en.md"

    def test_release_management_passthrough_outside_en(self):
        """release-management：不在 en/ 下的文件原样透传（不覆盖中文）。"""
        assert index._target_rel_path(
            "CANN", "release-management", "README.md") == "README.md"


def _ok_response(payload=None):
    """构造一个 200 响应的 MagicMock（_do_request 返回 r.json()）。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = payload or {}
    return resp


class TestNetworkErrorRetry:
    """网络层异常（读超时/断连）在 _do_request 内重试，耗尽后包装为
    AtomGitError（status_code=0），让单仓失败不中断整个批次。"""

    def test_retry_then_success(self):
        ok = _ok_response({"id": 1})
        with patch("translator.client._throttle"), \
                patch("translator.client.time.sleep") as sleep_mock, \
                patch("translator.client.requests.request",
                      side_effect=[requests.exceptions.ReadTimeout("t"), ok]) \
                as req_mock:
            result = client._do_request("POST", "http://api/pulls", "tok")
        assert result == {"id": 1}
        assert req_mock.call_count == 2
        sleep_mock.assert_called_once()

    def test_retry_exhausted_raises_atomgit_error(self):
        with patch("translator.client._throttle"), \
                patch("translator.client.time.sleep"), \
                patch("translator.client.requests.request",
                      side_effect=requests.exceptions.ConnectionError("down")):
            with pytest.raises(AtomGitError) as ei:
                client._do_request("POST", "http://api/pulls", "tok")
        assert "网络错误" in str(ei.value)

    def test_create_pr_uses_slow_timeout(self):
        """创建 PR 属慢操作（服务端逐文件计算大 PR 变更），用 SLOW_TIMEOUT。"""
        ok = _ok_response({"number": 1})
        with patch("translator.client._throttle"), \
                patch("translator.client.requests.request",
                      return_value=ok) as req_mock:
            client.create_pr("cann", "docs", "t", "u:br", "master", "b", "tok",
                             fork_path="u/docs")
        assert req_mock.call_args.kwargs["timeout"] == client.SLOW_TIMEOUT


class TestPaginateBrokenPaging:
    """AtomGit 部分接口（PR files）忽略分页参数，每页都返回同一份全量，
    _paginate 必须终止，否则以限流速率无限打请求（#80 卡死一小时）。"""

    @staticmethod
    def _file_entry(i):
        return {"filename": f"docs/en/f{i}.md", "status": "added"}

    def test_full_page_each_time_returns_batch_once(self):
        """返回条数 > per_page：服务端一次性返回全量，直接返回该批。"""
        full = [self._file_entry(i) for i in range(120)]
        with patch("translator.client._throttle"), \
                patch("translator.client.requests.request",
                      side_effect=lambda *a, **k: _ok_response(full)) as req:
            result = client._paginate("http://api/files", "tok", {})
        assert len(result) == 120
        assert req.call_count == 1

    def test_same_first_item_each_page_stops_loop(self):
        """返回条数 == per_page 但 page 参数被忽略（每页内容相同）：
        第二页首条与第一页相同时终止，不重复计入。"""
        same_page = [self._file_entry(i) for i in range(100)]
        with patch("translator.client._throttle"), \
                patch("translator.client.requests.request",
                      side_effect=lambda *a, **k: _ok_response(same_page)) as req:
            result = client._paginate("http://api/files", "tok", {})
        assert len(result) == 100
        assert req.call_count == 2

    def test_normal_multi_page_pagination_still_works(self):
        """常规分页不受影响：逐页取到不足一整页为止。
        （平台单页上限 100，合法分页响应不会超过 per_page。）"""
        pages = [[self._file_entry(i) for i in range(100)],
                 [self._file_entry(i) for i in range(100, 140)]]
        with patch("translator.client._throttle"), \
                patch("translator.client.requests.request",
                      side_effect=[_ok_response(p) for p in pages]) as req:
            result = client._paginate("http://api/branches", "tok", {})
        assert len(result) == 140
        assert req.call_count == 2
