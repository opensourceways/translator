"""服务配置：从环境变量加载 Settings。

凭据项（TR_WEBHOOK_TOKEN / TR_ADMIN_TOKEN / TR_DASH_TOKEN）优先读 secrets
文件（/vault/secrets/.env，见 translator.secrets），文件中无该键时回退环境
变量——生产环境凭据不进进程环境。GITCODE_TOKEN / GITCODE_SOURCE_REPO 由引擎
（translator）自行读取，本模块不接管。TR_SHADOW 仅作影子开关的初始值种子
——运行时真值存 SQLite config 表（见 db.is_shadow/set_shadow），支持 POST
/api/config/shadow 运行时切换并持久化，env 只在库中无记录时生效。
"""

import os
from dataclasses import dataclass

from translator import secrets

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _parse_bool(raw, default):
    """解析布尔环境变量，无法识别时回落默认值。"""
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in _TRUE_VALUES:
        return True
    if v in _FALSE_VALUES:
        return False
    return default


def _parse_int(raw, default):
    """解析整数环境变量，非法值回落默认值。"""
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class Settings:
    """常驻服务的全部配置项。

    Attributes:
        data_dir: SQLite 库与 run 工作目录的根目录（TR_DATA_DIR）
        port: HTTP 监听端口（TR_PORT）
        poll_interval: Poller 轮询间隔秒数（TR_POLL_INTERVAL）
        webhook_token: webhook 鉴权 token（TR_WEBHOOK_TOKEN）
        admin_token: 管理端点 token，空则管理端点放开（TR_ADMIN_TOKEN）
        dash_token: 只读端点 token，空则只读端点放开（TR_DASH_TOKEN）
        shadow: 影子模式初始值（TR_SHADOW，默认 true 安全优先），
            仅用于给 SQLite config 表播种，之后以库中值为准
    """

    data_dir: str = "./data"
    port: int = 8000
    poll_interval: int = 300
    webhook_token: str = ""
    admin_token: str = ""
    dash_token: str = ""
    shadow: bool = True

    @classmethod
    def from_env(cls, env=None, secrets_map=None):
        """构造 Settings：非敏感项读环境变量，凭据项优先读 secrets 文件。

        env 默认 os.environ；secrets_map 默认读 secrets 文件
        （/vault/secrets/.env），两者均可注入字典便于测试。secrets 文件存在
        但不可读时 load_env 抛 RuntimeError，启动即失败（fail-fast）。
        """
        env = os.environ if env is None else env
        if secrets_map is None:
            secrets_map = secrets.load_env()

        def pick(key, default=""):
            return secrets_map[key] if key in secrets_map else env.get(key, default)

        return cls(
            data_dir=env.get("TR_DATA_DIR", "./data"),
            port=_parse_int(env.get("TR_PORT"), 8000),
            poll_interval=_parse_int(env.get("TR_POLL_INTERVAL"), 300),
            webhook_token=pick("TR_WEBHOOK_TOKEN", ""),
            admin_token=pick("TR_ADMIN_TOKEN", ""),
            dash_token=pick("TR_DASH_TOKEN", ""),
            shadow=_parse_bool(env.get("TR_SHADOW"), True),
        )

    @property
    def db_path(self):
        """SQLite 库文件路径。"""
        return os.path.join(self.data_dir, "translator.db")

    @property
    def work_dir(self):
        """run 工作目录（克隆来源仓等临时产物）的根。"""
        return os.path.join(self.data_dir, "work")
