"""容器内分层自检：一条命令回答「哪一层断了」。

用法（容器内）::

    python3 -m webapp.doctor              # 进程/端口/配置/凭据/数据盘
    python3 -m webapp.doctor --network    # 额外检查 AtomGit 可达性
    python3 -m webapp.doctor --json       # 机器可读，供监控采集

设计要点：
- 只读状态、不做业务动作（不克隆、不提交），因此可用于故障现场
- 每层独立判定，前一层失败不影响后面的检查继续跑
- 凭据只报「在不在、权限对不对」，绝不打印值
- 退出码：0 全部通过（含跳过/告警），1 存在故障
"""

import argparse
import json
import os
import shutil
import sqlite3
import stat
import sys
import unicodedata
import urllib.error
import urllib.request

from translator import secrets
from webapp.config import Settings

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

# 容器内服务的默认监听地址（uvicorn 命令固定 --port 8000）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# AtomGit API 根地址：能收到任意 HTTP 响应即说明出网正常
NETWORK_PROBE_URL = "https://api.atomgit.com"


class Result:
    """一层检查的结果：层名 + 状态 + 说明。"""

    __slots__ = ("layer", "status", "detail")

    def __init__(self, layer, status, detail):
        self.layer = layer
        self.status = status
        self.detail = detail

    def as_dict(self):
        return {"layer": self.layer, "status": self.status,
                "detail": self.detail}


def check_process():
    """第 1 层：PID 1 是不是 uvicorn（容器主进程还在不在）。"""
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return Result("进程", SKIP, "非 Linux 环境，跳过 /proc 检查")
    cmd = " ".join(cmd.split())
    if not cmd:
        return Result("进程", WARN, "PID 1 命令行为空，无法确认主进程")
    if "uvicorn" not in cmd:
        return Result("进程", WARN, f"主进程不是 uvicorn: {cmd[:80]}")
    return Result("进程", OK, cmd[:96])


def check_port(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=3.0):
    """第 2 层：容器内能不能通过 HTTP 打通服务端口。"""
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
            status = resp.status
    except urllib.error.HTTPError as exc:
        return Result("端口", FAIL, f"{url} 返回 HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - 连接失败原因需要原样展示
        return Result("端口", FAIL, f"{url} 连接失败: {exc}")
    if status != 200:
        return Result("端口", FAIL, f"{url} 返回 HTTP {status}")
    return Result("端口", OK, f"{url} 200 {body[:80]}")


def check_config():
    """第 3 层：配置项读到了没有、是否合理。"""
    try:
        settings = Settings.from_env()
    except Exception as exc:  # noqa: BLE001
        return Result("配置", FAIL, f"读取配置失败: {exc}")
    detail = (f"data_dir={settings.data_dir} wait={settings.wait_timeout}s "
              f"max_paths={settings.max_paths} queue={settings.queue_max} "
              f"retention={settings.retention_days}d")
    return Result("配置", OK, detail)


def _secrets_path(path=None):
    """凭据文件路径：入参 > TR_SECRETS_FILE > 默认 Vault 路径。"""
    if path:
        return path
    return os.environ.get("TR_SECRETS_FILE", "").strip() \
        or secrets.DEFAULT_SECRETS_FILE


def check_secrets(path=None):
    """第 4 层：凭据文件在不在、权限对不对、两个键齐不齐。

    只报告键是否存在与长度，绝不输出值本身。
    """
    path = _secrets_path(path)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return Result("凭据", FAIL,
                      f"{path} 不存在（生产应由 Vault 注入；本地开发可忽略）")
    except OSError as exc:
        return Result("凭据", FAIL, f"{path} 不可访问: {exc}")

    mode = stat.S_IMODE(info.st_mode)
    owner = f"{info.st_uid}:{info.st_gid}"
    try:
        data = secrets.load_env(path)
    except RuntimeError as exc:
        return Result("凭据", FAIL, f"{path} 存在但读取失败: {exc}")

    missing = [key for key in ("TR_API_KEY", "ATOMGIT_TOKEN")
               if not (data.get(key) or "").strip()]
    if missing:
        return Result("凭据", FAIL,
                      f"{path} {mode:04o} {owner}，缺少键: {', '.join(missing)}")
    sizes = " ".join(f"{key}={len(data[key])}字"
                     for key in ("TR_API_KEY", "ATOMGIT_TOKEN"))
    return Result("凭据", OK, f"{path} {mode:04o} {owner}，{sizes}")


def check_data(data_dir):
    """第 5 层：数据目录可写、SQLite 能打开、剩余空间够。

    本地开发的 data_dir 可能尚未建库（表还没建），此时按告警处理，
    容器里则说明启动阶段没跑起来，属于故障。
    """
    if not os.path.isdir(data_dir):
        return Result("数据盘", FAIL, f"{data_dir} 不存在或不是目录")

    probe = os.path.join(data_dir, ".doctor-probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        return Result("数据盘", FAIL, f"{data_dir} 不可写: {exc}")

    free_gb = shutil.disk_usage(data_dir).free / (1024 ** 3)
    db_path = os.path.join(data_dir, "translator.db")
    if not os.path.exists(db_path):
        return Result("数据盘", WARN,
                      f"{data_dir} 可写，但 {db_path} 尚未创建"
                      f"（服务未启动过），剩余 {free_gb:.1f} GB")
    try:
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM submissions").fetchone()[0]
    except sqlite3.Error as exc:
        return Result("数据盘", FAIL, f"{db_path} 打开失败: {exc}")
    return Result("数据盘", OK,
                  f"{data_dir} 可写，submissions={count}，剩余 {free_gb:.1f} GB")


def check_network(timeout=5.0):
    """第 6 层（可选）：出网是否正常。

    对 AtomGit 根路径发一个无 token 请求，能收到任意 HTTP 响应（含 401）
    就说明网络通；只有连不上/超时才算故障。
    """
    try:
        with urllib.request.urlopen(NETWORK_PROBE_URL, timeout=timeout) as resp:
            return Result("外网", OK,
                          f"{NETWORK_PROBE_URL} 可达（HTTP {resp.status}）")
    except urllib.error.HTTPError as exc:
        return Result("外网", OK,
                      f"{NETWORK_PROBE_URL} 可达（HTTP {exc.code}，未带 token 属正常）")
    except Exception as exc:  # noqa: BLE001
        return Result("外网", FAIL, f"{NETWORK_PROBE_URL} 访问失败: {exc}")


def _width(text):
    """按终端显示宽度计数（中日韩字符占两列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def _pad(text, width):
    """按显示宽度右侧补空格。"""
    return text + " " * max(width - _width(text), 0)


def run(host=DEFAULT_HOST, port=DEFAULT_PORT, data_dir=None,
        with_network=False, timeout=3.0):
    """按层执行检查并返回结果列表（不做任何业务动作）。"""
    if data_dir is None:
        try:
            data_dir = Settings.from_env().data_dir
        except Exception:  # noqa: BLE001 - 配置坏掉时仍要能跑完其余层
            data_dir = "./data"
    results = [
        check_process(),
        check_port(host, port, timeout),
        check_config(),
        check_secrets(),
        check_data(data_dir),
    ]
    if with_network:
        results.append(check_network(timeout=max(timeout, 5.0)))
    return results


def render(results):
    """把结果渲染为对齐的文本表格。"""
    lines = ["translator 容器自检", "-" * 64]
    for index, item in enumerate(results, 1):
        head = _pad(f"[{index}] {item.layer}", 12)
        lines.append(f"{head}{_pad(item.status, 6)}{item.detail}")
    return "\n".join(lines)


def summarize(results):
    """返回 (是否通过, 结论文案)。"""
    failed = [r for r in results if r.status == FAIL]
    warned = [r for r in results if r.status == WARN]
    if failed:
        layers = "、".join(r.layer for r in failed)
        return False, f"结论：{len(failed)} 项故障（{layers}），见上方明细"
    if warned:
        layers = "、".join(r.layer for r in warned)
        return True, f"结论：基本正常，{len(warned)} 项告警（{layers}）"
    return True, ("结论：全部通过（仅证明服务自身状态，"
                  "业务链路请再跑一次 dry_run 提交确认）")


def main(argv=None):
    """命令行入口；存在 FAIL 时返回 1，便于脚本/探针判断。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m webapp.doctor",
        description="容器内分层自检：进程 / 端口 / 配置 / 凭据 / 数据盘 / 外网")
    parser.add_argument("--host", default=DEFAULT_HOST, help="服务监听地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="服务监听端口")
    parser.add_argument("--data-dir", default=None, help="覆盖数据目录")
    parser.add_argument("--network", action="store_true",
                        help="额外检查 AtomGit 可达性")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="单次网络请求超时（秒）")
    args = parser.parse_args(argv)

    results = run(host=args.host, port=args.port, data_dir=args.data_dir,
                  with_network=args.network, timeout=args.timeout)
    passed, verdict = summarize(results)

    if args.json:
        print(json.dumps({
            "ok": passed,
            "verdict": verdict,
            "results": [item.as_dict() for item in results],
        }, ensure_ascii=False, indent=2))
    else:
        print(render(results))
        print(verdict)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
