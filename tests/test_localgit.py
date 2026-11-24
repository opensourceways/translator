"""localgit 模块的单元测试。

覆盖本地 git 集成：克隆命令构造、ls-tree 解析、cat-file 读内容。
测试通过 mock subprocess 拦截，不执行真实 git 命令。
"""

import os
import subprocess
from unittest.mock import patch

import pytest

from translator import localgit
from translator.client import AtomGitError


class TestRepoUrl:
    def test_url_has_no_credentials(self):
        url = localgit._repo_url("Ascend", "msprof")
        assert url == "https://atomgit.com/Ascend/msprof.git"
        assert "@" not in url


class TestGitAuth:
    def test_token_lives_in_file_not_env(self):
        with localgit._git_auth("s3cr3t-token") as env:
            assert "s3cr3t-token" not in " ".join(str(v) for v in env.values())
            script = env["GIT_ASKPASS"]
            assert os.path.exists(script)
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            body = open(script, encoding="utf-8").read()
            assert "oauth2" in body and "cat" in body
            # token 文件与脚本同目录，仅属主可读
            token_path = os.path.join(os.path.dirname(script), "token")
            with open(token_path, encoding="utf-8") as fh:
                assert fh.read() == "s3cr3t-token"
        assert not os.path.exists(script)      # 用完清理


class TestClone:
    def _clone(self, *args, **kwargs):
        kwargs.setdefault("log", lambda m: None)
        return localgit.clone(*args, **kwargs)

    def test_partial_clone_command(self, tmp_path):
        dest = str(tmp_path / "d")
        with patch("translator.localgit.subprocess.run") as run_mock:
            self._clone("Ascend", "msprof", "26.1.0", "tok", dest)
            cmd = run_mock.call_args.args[0]
            assert cmd[:2] == ["git", "clone"]
            assert "--depth=1" in cmd and "--no-checkout" in cmd
            assert "--filter=blob:none" in cmd
            assert "--branch" in cmd and "26.1.0" in cmd
            assert cmd[-1] == dest
            assert cmd[-2] == "https://atomgit.com/Ascend/msprof.git"
            # token 既不在 URL 也不在任何参数里
            assert "tok" not in " ".join(cmd)
            env = run_mock.call_args.kwargs["env"]
            assert env["GIT_ASKPASS"] and env["GIT_TERMINAL_PROMPT"] == "0"

    def test_full_clone_with_blobs(self, tmp_path):
        """blobs=True 时不加 --filter，保留全部 blob（内容本地读取）。"""
        with patch("translator.localgit.subprocess.run") as run_mock:
            self._clone("Ascend", "msprof", "26.1.0", "tok",
                        str(tmp_path / "d"), blobs=True)
            cmd = run_mock.call_args.args[0]
            assert "--filter=blob:none" not in cmd

    def test_empty_ref_clones_default_branch(self, tmp_path):
        with patch("translator.localgit.subprocess.run") as run_mock:
            self._clone("Ascend", "msprof", "", "tok", str(tmp_path / "d"))
            cmd = run_mock.call_args.args[0]
            assert "--branch" not in cmd

    def test_removes_existing_dir(self, tmp_path):
        dest = str(tmp_path / "d")
        with patch("translator.localgit.subprocess.run"), \
                patch("translator.localgit.shutil.rmtree") as rm_mock, \
                patch("translator.localgit.os.path.isdir",
                      return_value=True):
            self._clone("Ascend", "msprof", "26.1.0", "tok", dest)
            rm_mock.assert_any_call(dest)

    def test_failure_raises_atomgit_error(self, tmp_path):
        err = subprocess.CalledProcessError(
            128, "git clone", stderr="fatal: Repository not found")
        with patch("translator.localgit.subprocess.run",
                   side_effect=err):
            with pytest.raises(AtomGitError) as exc_info:
                self._clone("Ascend", "msprof", "26.1.0", "tok",
                            str(tmp_path / "d"))
            assert "Repository not found" in str(exc_info.value)


class TestTreeMap:
    def test_parses_ls_tree(self):
        out = (
            "100644 blob 1111111111111111111111111111111111111111\tdocs/en/a.md\n"
            "100644 blob 2222222222222222222222222222222222222222\tdocs/en/b.png\n"
            "040000 tree 3333333333333333333333333333333333333333\tsome/dir\n"
        )
        with patch("translator.localgit._run_git", return_value=out):
            tree = localgit.tree_map("/repo")
            assert tree == {
                "docs/en/a.md": "1" * 40,
                "docs/en/b.png": "2" * 40,
            }

class TestReadFile:
    def test_missing_file_maps_to_404(self):
        with patch("translator.localgit._run_git_bytes",
                   side_effect=AtomGitError(200, "git show 失败")):
            with pytest.raises(AtomGitError) as exc_info:
                localgit.read_file("/repo", "nope")
            assert exc_info.value.status_code == 404

    def test_returns_raw_bytes(self):
        with patch("translator.localgit._run_git_bytes",
                   return_value=b'# hello'):
            assert localgit.read_file(
                "/repo", "README.md") == b'# hello'


class TestQuotePath:
    """git 默认 core.quotepath=true 会把中文路径输出为 C 风格转义串
    （"…\\346\\233\\264…"），解析后会拼出不存在的 org/repo；所有 git
    子命令必须显式带 -c core.quotepath=false。"""

    def test_run_git_bytes_disables_quotepath(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0,
                                                stdout=b"", stderr=b"")
        with patch("translator.localgit.subprocess.run",
                   return_value=completed) as run_mock:
            localgit._run_git_bytes("/repo", "ls-tree", "-r", "HEAD")
            cmd = run_mock.call_args.args[0]
            assert cmd[:4] == ["git", "-c", "core.quotepath=false", "-C"]
