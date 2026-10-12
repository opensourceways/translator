"""凭据读取：优先 /vault/secrets/.env 文件，其次环境变量。

安全目标（上线 checklist「secret 不应明文保存在容器环境」）：启动脚本不再把
Vault 注入的 secrets 文件 `source` 成环境变量，改由应用按需直接读文件，收敛
`/proc/<pid>/environ`、`ps e` 以及子进程继承导致的凭据暴露面。

回退语义：文件不存在（本地/compose 场景）时回退 os.environ，既有用法与测试
不变；文件存在但不可读（权限/挂载异常）则抛 RuntimeError——fail-fast，避免
静默以空凭据启动。
"""

import os
import re

# Vault Agent Injector 注入的 secrets 文件路径（参照 jenkins-log-viewer 先例）
DEFAULT_SECRETS_FILE = "/vault/secrets/.env"

# 命名 token 的键前缀：Vault 中形如 TR_TOKEN_<名称大写>，服务只保存名称
TOKEN_PREFIX = "TR_TOKEN_"

# 名称只允许小写字母开头，后接小写字母/数字/下划线（与 env 变量名兼容）
_TOKEN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# mtime 缓存：同一文件未变更时不重复读盘（get 会被高频调用）
_cache = {"path": None, "mtime": None, "data": {}}


def _secrets_path(path=None):
    """返回 secrets 文件路径：显式入参 > TR_SECRETS_FILE > 默认路径。"""
    if path:
        return path
    return os.environ.get("TR_SECRETS_FILE", "").strip() or DEFAULT_SECRETS_FILE


def _parse(text):
    """解析 .env 文本：忽略空行与 # 注释，支持可选 export 前缀与成对引号。"""
    data = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            data[key] = value
    return data


def load_env(path=None):
    """读取 secrets 文件并返回键值字典（按 mtime 缓存）。

    文件不存在返回空字典（调用方回退环境变量）；存在但不可访问/不可读抛
    RuntimeError，由启动路径上抛以实现 fail-fast。
    """
    path = _secrets_path(path)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError(
            f"secrets 文件 {path} 不可访问（权限或挂载异常）: {exc}") from exc
    if _cache["path"] == path and _cache["mtime"] == stat.st_mtime:
        return _cache["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise RuntimeError(
            f"secrets 文件 {path} 存在但不可读: {exc}") from exc
    data = _parse(text)
    _cache.update(path=path, mtime=stat.st_mtime, data=data)
    return data


def get(name, default=""):
    """取配置项：secrets 文件优先，其次环境变量，最后 default。"""
    value = load_env().get(name)
    if value is not None:
        return value
    return os.environ.get(name, default)


def is_valid_token_name(name):
    """校验命名 token 名称：小写字母开头，仅含小写字母/数字/下划线。

    名称会拼成 Vault 键 TR_TOKEN_<名称大写>，故限制为 env 变量名可用的字符集。
    """
    return bool(_TOKEN_NAME_RE.match((name or "").strip()))


def get_named_token(name):
    """按名称取命名 token 值：读 Vault 键 TR_TOKEN_<名称大写>。

    名称为空或非法、或对应键未配置时返回空串。
    """
    name = (name or "").strip()
    if not is_valid_token_name(name):
        return ""
    return get(TOKEN_PREFIX + name.upper(), "").strip()


def list_token_names():
    """枚举已配置的命名 token 名称（Vault 文件键 ∪ 环境变量键），升序去重。

    只返回名称合法且已配置非空值的条目；服务前端据此渲染下拉框。
    """
    keys = set(load_env().keys()) | set(os.environ.keys())
    names = set()
    for key in keys:
        if not key.startswith(TOKEN_PREFIX):
            continue
        name = key[len(TOKEN_PREFIX):].strip().lower()
        if is_valid_token_name(name) and get_named_token(name):
            names.add(name)
    return sorted(names)


def list_token_values():
    """返回全部命名 token 的值（供日志脱敏），不含键名。"""
    return [v for v in (get_named_token(n) for n in list_token_names()) if v]
