"""服务配置：从环境变量加载 Settings。

服务只提供 HTTP API：调用方传 AtomGit token 和权威仓文件路径，服务读取
权威仓内容后向目标社区仓提交 PR。凭据（ATOMGIT_TOKEN）由引擎经
translator.secrets 读取（secrets 文件优先、环境变量回退），本模块不接管。
"""

import os
from dataclasses import dataclass


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
    """常驻 API 服务的全部配置项。

    Attributes:
        data_dir: SQLite 库与任务工作目录的根目录（TR_DATA_DIR）
        port: HTTP 监听端口（TR_PORT）
        wait_timeout: POST /submissions 带 wait=true 时的最长等待秒数
            （TR_WAIT_TIMEOUT）
        max_paths: 单次提交请求允许的最大文件路径数（TR_MAX_PATHS）
        queue_max: 待执行提交任务队列容量（TR_QUEUE_MAX）
        retention_days: 提交任务与结果保留天数（TR_SUBMISSION_RETENTION_DAYS）
        api_key: 服务级 API Key（TR_API_KEY）；为空时 create_app 回退从
            translator.secrets 读同名键（Vault 注入文件优先），仍为空则
            拒绝启动
    """

    data_dir: str = "./data"
    port: int = 8000
    wait_timeout: int = 30
    max_paths: int = 200
    queue_max: int = 100
    retention_days: int = 90
    api_key: str = ""

    @classmethod
    def from_env(cls, env=None):
        """构造 Settings，全部配置项均来自非敏感环境变量。"""
        env = os.environ if env is None else env
        return cls(
            data_dir=env.get("TR_DATA_DIR", "./data"),
            port=_parse_int(env.get("TR_PORT"), 8000),
            wait_timeout=_parse_int(env.get("TR_WAIT_TIMEOUT"), 30),
            max_paths=_parse_int(env.get("TR_MAX_PATHS"), 200),
            queue_max=_parse_int(env.get("TR_QUEUE_MAX"), 100),
            retention_days=_parse_int(
                env.get("TR_SUBMISSION_RETENTION_DAYS"), 90),
            api_key=env.get("TR_API_KEY", "").strip(),
        )

    @property
    def db_path(self):
        """SQLite 库文件路径。"""
        return os.path.join(self.data_dir, "translator.db")

    @property
    def work_dir(self):
        """提交任务工作目录（克隆权威仓等临时产物）的根。"""
        return os.path.join(self.data_dir, "work")
