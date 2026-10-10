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
        "GITCODE_TOKEN=abc123\n"
        'export TR_ADMIN_TOKEN="quoted"\n'
        "TR_DASH_TOKEN='single'\n"
        "NO_EQUALS_LINE\n"
    )
    assert secrets.load_env(_write(tmp_path / ".env", text)) == {
        "GITCODE_TOKEN": "abc123",
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
        tmp_path / ".env", "GITCODE_TOKEN=from-file\n"))
    monkeypatch.setenv("GITCODE_TOKEN", "from-env")
    assert secrets.get("GITCODE_TOKEN") == "from-file"


def test_get_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("GITCODE_TOKEN", "from-env")
    assert secrets.get("GITCODE_TOKEN") == "from-env"


def test_get_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("TR_SECRETS_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("GITCODE_TOKEN", raising=False)
    assert secrets.get("GITCODE_TOKEN", "fallback") == "fallback"


def test_get_reloads_on_file_change(tmp_path, monkeypatch):
    """文件变更（mtime 变化）后取到新值，缓存不会滞留旧值。"""
    path = tmp_path / ".env"
    monkeypatch.setenv("TR_SECRETS_FILE", str(path))
    _write(path, "GITCODE_TOKEN=v1\n")
    assert secrets.get("GITCODE_TOKEN") == "v1"
    _write(path, "GITCODE_TOKEN=v2\n")
    os.utime(path, (1, 1))
    assert secrets.get("GITCODE_TOKEN") == "v2"


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
