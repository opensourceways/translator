"""webapp.doctor：容器内分层自检（全部本地执行，不触网）。"""

import json
import os
import sqlite3
import urllib.error

import pytest

from webapp import doctor


class TestIndividualChecks:
    def test_process_reports_a_status(self):
        result = doctor.check_process()
        assert result.status in (doctor.OK, doctor.WARN, doctor.SKIP)
        assert result.layer == "进程"

    def test_config_ok(self):
        result = doctor.check_config()
        assert result.status == doctor.OK
        assert "data_dir=" in result.detail

    def test_port_refused(self):
        """没有服务监听时应判为故障而不是异常退出。"""
        result = doctor.check_port(port=1, timeout=0.5)
        assert result.status == doctor.FAIL
        assert result.layer == "端口"

    def test_secrets_file_missing(self, tmp_path):
        result = doctor.check_secrets(str(tmp_path / "absent.env"))
        assert result.status == doctor.FAIL
        assert "不存在" in result.detail

    def test_secrets_complete(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("TR_API_KEY=s3cr3t-value\n"
                        "ATOMGIT_TOKEN=tok3n-value\n", encoding="utf-8")
        os.chmod(path, 0o400)
        result = doctor.check_secrets(str(path))
        assert result.status == doctor.OK
        assert "TR_API_KEY=" in result.detail
        assert "s3cr3t-value" not in result.detail
        assert "tok3n-value" not in result.detail

    def test_secrets_missing_key(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("TR_API_KEY=k\n", encoding="utf-8")
        result = doctor.check_secrets(str(path))
        assert result.status == doctor.FAIL
        assert "ATOMGIT_TOKEN" in result.detail

    def test_data_dir_missing(self, tmp_path):
        result = doctor.check_data(str(tmp_path / "nope"))
        assert result.status == doctor.FAIL

    def test_data_dir_without_db_is_warning(self, tmp_path):
        result = doctor.check_data(str(tmp_path))
        assert result.status == doctor.WARN
        assert "尚未创建" in result.detail

    def test_data_dir_with_db(self, tmp_path):
        db_path = tmp_path / "translator.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE submissions (id INTEGER)")
            conn.execute("INSERT INTO submissions (id) VALUES (1)")
            conn.execute("INSERT INTO submissions (id) VALUES (2)")
        result = doctor.check_data(str(tmp_path))
        assert result.status == doctor.OK
        assert "submissions=2" in result.detail

    def test_network_http_error_counts_as_reachable(self, monkeypatch):
        """401/403 说明网络通，只有连不上才算故障。"""
        def boom(url, timeout=0):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
        result = doctor.check_network()
        assert result.status == doctor.OK
        assert "401" in result.detail

    def test_network_unreachable(self, monkeypatch):
        def boom(url, timeout=0):
            raise OSError("network unreachable")

        monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
        result = doctor.check_network()
        assert result.status == doctor.FAIL


def _patch_all_ok(monkeypatch):
    for name, layer in (("check_process", "进程"), ("check_port", "端口"),
                        ("check_config", "配置"), ("check_secrets", "凭据"),
                        ("check_data", "数据盘")):
        monkeypatch.setattr(doctor, name,
                            lambda *a, _layer=layer, **k: doctor.Result(
                                _layer, doctor.OK, "ok"))


class TestMain:
    def test_all_ok_returns_zero(self, monkeypatch, capsys):
        _patch_all_ok(monkeypatch)
        assert doctor.main([]) == 0
        out = capsys.readouterr().out
        assert "全部通过" in out
        assert "[1] 进程" in out

    def test_failure_returns_one(self, monkeypatch, capsys):
        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(doctor, "check_secrets",
                            lambda: doctor.Result("凭据", doctor.FAIL, "缺 key"))
        assert doctor.main([]) == 1
        out = capsys.readouterr().out
        assert "1 项故障" in out
        assert "凭据" in out

    def test_json_output(self, monkeypatch, capsys):
        _patch_all_ok(monkeypatch)
        assert doctor.main(["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is True
        assert [item["layer"] for item in payload["results"]] == [
            "进程", "端口", "配置", "凭据", "数据盘"]

    def test_network_flag_adds_layer(self, monkeypatch, capsys):
        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(doctor, "check_network",
                            lambda timeout=0: doctor.Result(
                                "外网", doctor.OK, "reachable"))
        assert doctor.main(["--network", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][-1]["layer"] == "外网"

    def test_data_dir_override(self, monkeypatch, tmp_path):
        calls = {}

        def fake_data_dir(path):
            calls["path"] = path
            return doctor.Result("数据盘", doctor.OK, "ok")

        _patch_all_ok(monkeypatch)
        monkeypatch.setattr(doctor, "check_data", fake_data_dir)
        assert doctor.main(["--data-dir", str(tmp_path)]) == 0
        assert calls["path"] == str(tmp_path)
