"""translator.secrets：凭据读取顺序（文件 > 环境变量）、fail-fast 与解析。"""

import os

import pytest

from translator import secrets
from webapp.config import Settings


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个用例前后清空 mtime 缓存，避免用例之间串味。"""
    secrets._cache.update(path=None, mtime=None, data={})
    yield
    secrets._cache.update(path=None, mtime=None, data={})


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_env_missing_file_returns_empty(tmp_path):
    """文件不存在 → 空字典（调用方回退环境变量，本地/compose 场景）。"""
    assert secrets.load_env(str(tmp_path / "absent.env")) == {}


def test_load_env_parses_comments_quotes_and_export(tmp_path):
    """忽略空行/注释，支持 export 前缀与成对引号。"""
    text = (
        "# 注释\n"
        "\n"
        "ATOMGIT_TOKEN=abc123\n"
        'export TR_ADMIN_TOKEN="quoted"\n'
        "TR_DASH_TOKEN='single'\n"
        "NO_EQUALS_LINE\n"
    )
    assert secrets.load_env(_write(tmp_path / ".env", text)) == {
        "ATOMGIT_TOKEN": "abc123",
        "TR_ADMIN_TOKEN": "quoted",
        "TR_DASH_TOKEN": "single",
    }


def test_load_env_unreadable_path_raises(tmp_path):
    """路径存在但无法作为文件读取 → RuntimeError（fail-fast）。"""
    path = tmp_path / ".env"
    path.mkdir()
    with pytest.raises(RuntimeError):
        secrets.load_env(str(path))


def test_get_prefers_file_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_SECRETS_FILE", _write(
        tmp_path / ".env", "ATOMGIT_TOKEN=from-file\n"))
    monkeypatch.setenv("ATOMGIT_TOKEN", "from-env")
    assert secrets.get("ATOMGIT_TOKEN") == "from-file"


def test_get_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("ATOMGIT_TOKEN", "from-env")
    assert secrets.get("ATOMGIT_TOKEN") == "from-env"


def test_get_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("ATOMGIT_TOKEN", raising=False)
    assert secrets.get("ATOMGIT_TOKEN", "fallback") == "fallback"


def test_get_reloads_on_file_change(tmp_path, monkeypatch):
    """文件变更（mtime 变化）后取到新值，缓存不会滞留旧值。"""
    path = tmp_path / ".env"
    monkeypatch.setenv("TR_SECRETS_FILE", str(path))
    _write(path, "ATOMGIT_TOKEN=v1\n")
    assert secrets.get("ATOMGIT_TOKEN") == "v1"
    _write(path, "ATOMGIT_TOKEN=v2\n")
    os.utime(path, (1, 1))
    assert secrets.get("ATOMGIT_TOKEN") == "v2"


def test_settings_prefers_secrets_over_env(tmp_path):
    """Settings 凭据项优先取 secrets 文件值，非凭据项仍读 env。"""
    path = _write(tmp_path / ".env",
                  "TR_WEBHOOK_TOKEN=web-file\nTR_ADMIN_TOKEN=admin-file\n")
    settings = Settings.from_env(
        env={"TR_DATA_DIR": "/tmp/d", "TR_WEBHOOK_TOKEN": "web-env"},
        secrets_map=secrets.load_env(path))
    assert settings.webhook_token == "web-file"
    assert settings.admin_token == "admin-file"
    assert settings.dash_token == ""
    assert settings.data_dir == "/tmp/d"


def test_settings_falls_back_to_env_when_no_secrets():
    settings = Settings.from_env(env={"TR_WEBHOOK_TOKEN": "web-env"},
                                 secrets_map={})
    assert settings.webhook_token == "web-env"


class TestTokenNameValidation:
    """命名 token 名称字符集：小写字母开头，仅含小写字母/数字/下划线。"""

    def test_valid_names(self):
        assert secrets.is_valid_token_name("bot")
        assert secrets.is_valid_token_name("cann_robot")
        assert secrets.is_valid_token_name("bot2")
        assert secrets.is_valid_token_name("  bot_a  ")  # 前后空白忽略

    def test_invalid_names(self):
        assert not secrets.is_valid_token_name("")
        assert not secrets.is_valid_token_name(None)
        assert not secrets.is_valid_token_name("Bot")      # 大写
        assert not secrets.is_valid_token_name("bot-a")    # 连字符
        assert not secrets.is_valid_token_name("1bot")     # 数字开头
        assert not secrets.is_valid_token_name("bot.x")    # 点


class TestNamedTokens:
    """命名 token 取值与枚举（键 TR_TOKEN_<名称大写>，文件优先）。"""

    def test_get_named_token_from_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", _write(
            tmp_path / ".env", "TR_TOKEN_BOT_A=secret-1\n"))
        assert secrets.get_named_token("bot_a") == "secret-1"

    def test_get_named_token_missing_or_invalid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", _write(
            tmp_path / ".env", "TR_TOKEN_BOT=secret-1\n"))
        assert secrets.get_named_token("nope") == ""
        assert secrets.get_named_token("bot-a") == ""  # 非法名称
        assert secrets.get_named_token("") == ""

    def test_list_token_names_sorted_and_filtered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", _write(
            tmp_path / ".env",
            "TR_TOKEN_CANN_ROBOT=r\n"
            "TR_TOKEN_BOT=b\n"
            "TR_TOKEN_EMPTY=\n"          # 空值 → 不列出
            "TR_TOKEN_BAD-NAME=x\n"      # 名称非法 → 不列出
            "ATOMGIT_TOKEN=default\n"))  # 无前缀 → 不列出
        monkeypatch.setenv("TR_TOKEN_ENVONLY", "e")  # 环境变量来源
        # 清掉可能残留的其它 TR_TOKEN_*
        for key in list(os.environ):
            if key.startswith("TR_TOKEN_") and key != "TR_TOKEN_ENVONLY":
                monkeypatch.delenv(key, raising=False)
        assert secrets.list_token_names() == ["bot", "cann_robot", "envonly"]

    def test_list_token_values_for_masking(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TR_SECRETS_FILE", _write(
            tmp_path / ".env", "TR_TOKEN_BOT=b\nTR_TOKEN_CANN=r\n"))
        for key in list(os.environ):
            if key.startswith("TR_TOKEN_"):
                monkeypatch.delenv(key, raising=False)
        assert sorted(secrets.list_token_values()) == ["b", "r"]
