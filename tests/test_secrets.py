"""translator.secrets：凭据文件解析与读取优先级（文件 > 环境变量）。"""

import pytest

from translator import secrets


@pytest.fixture(autouse=True)
def _clear_cache():
    """清空模块级 mtime 缓存，避免用例之间互相干扰。"""
    secrets._cache.update(path=None, mtime=None, data={})
    yield
    secrets._cache.update(path=None, mtime=None, data={})


class TestParse:
    def test_skips_comments_and_blank_lines(self):
        data = secrets._parse("# 注释\n\nA=1\n  \nB=2\n")
        assert data == {"A": "1", "B": "2"}

    def test_supports_export_and_quotes(self):
        data = secrets._parse("export A=1\nB='2'\nC=\"3\"\n")
        assert data == {"A": "1", "B": "2", "C": "3"}

    def test_ignores_lines_without_equals(self):
        assert secrets._parse("NOISE\nA=1\n") == {"A": "1"}

    def test_tolerates_utf8_bom(self):
        """Windows 记事本写的 .env 带 BOM，不能把首个键读成 \\ufeffTR_API_KEY。"""
        data = secrets._parse("\ufeffTR_API_KEY=abc\nATOMGIT_TOKEN=def\n")
        assert data["TR_API_KEY"] == "abc"
        assert data["ATOMGIT_TOKEN"] == "def"

    def test_keeps_value_containing_equals(self):
        assert secrets._parse("A=b=c\n") == {"A": "b=c"}


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        assert secrets.load_env(str(tmp_path / "absent.env")) == {}

    def test_reads_file(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("TR_API_KEY=file-key\n", encoding="utf-8")
        assert secrets.load_env(str(path))["TR_API_KEY"] == "file-key"


class TestGet:
    def test_file_wins_over_environ(self, tmp_path, monkeypatch):
        path = tmp_path / ".env"
        path.write_text("TR_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.setenv("TR_SECRETS_FILE", str(path))
        monkeypatch.setenv("TR_API_KEY", "from-env")
        assert secrets.get("TR_API_KEY") == "from-file"

    def test_falls_back_to_environ_when_file_missing(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.setenv("TR_API_KEY", "from-env")
        assert secrets.get("TR_API_KEY") == "from-env"

    def test_default_used_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
        monkeypatch.delenv("TR_NOT_SET_ANYWHERE", raising=False)
        assert secrets.get("TR_NOT_SET_ANYWHERE", "fallback") == "fallback"
