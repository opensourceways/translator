"""service 各子模块共享的常量、来源仓库解析与通用工具。

rules / index / scan / submit 都从本模块获取共享定义，避免子模块之间
相互引用造成循环依赖：
- 仓库与路径常量（SOURCE_ORG / SOURCE_REPO、DEFAULT_RULES_PATH、
  MAX_TOTAL_SIZE、AUTO_BRANCH_PREFIX）
- _source_repo：译稿来源仓库的解析（支持 GITCODE_SOURCE_REPO 覆盖）
- ConfigError：配置损坏异常（权限边界配置不能静默放行）
- _git_blob_sha：git blob sha 计算（免下载内容对比）
"""

import hashlib
import os
import re

# 译稿权威来源仓库（默认）；可用 GITCODE_SOURCE_REPO 环境变量覆盖为
# 自己的 Fork（如 "lei0308/Computing_Ecosystem_Docs-EN"），便于在
# 没有权威仓库管理权限时测试（Webhook 配在 Fork 上）
SOURCE_ORG = "Computing_Ecosystem_Docs"
SOURCE_REPO = "Computing_Ecosystem_Docs-EN"
GIT_HOST = "https://gitcode.com"  # git 协议仓库地址（API 用 client.BASE_URL）

DEFAULT_RULES_PATH = "token_rules.json"  # 权威仓库根目录下的规则文件
MAX_TOTAL_SIZE = 5 * 1024 * 1024         # 单次上传累计文件大小上限 5 MB
AUTO_BRANCH_PREFIX = "translator/auto-"  # 自动提交的固定分支名前缀


class ConfigError(Exception):
    """规则配置损坏（JSON 非法等）时抛出——权限边界配置不能静默放行。"""

    pass


def _source_repo():
    """返回译稿来源仓库 (owner, repo)，优先取 GITCODE_SOURCE_REPO 环境变量。

    对环境变量的写法做宽容解析：允许粘贴完整 URL
    （https://gitcode.com/owner/repo）或带 .git 后缀，只取最后两段。
    """
    full = os.environ.get("GITCODE_SOURCE_REPO", "").strip()
    if full:
        full = re.sub(r"^https?://[^/]+/", "", full)
        full = full.removesuffix(".git").strip("/")
        parts = full.split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
    return SOURCE_ORG, SOURCE_REPO


def _git_blob_sha(content_bytes):
    """按 git 规则计算文件内容的 blob sha。

    git 对 blob 对象的 sha 计算方式为 sha1("blob <字节数>\\0" + 内容)，
    与 contents API 返回的文件 sha 一致，可直接比对判断目标仓库
    文件与译稿内容是否相同，避免下载目标文件全文。
    """
    header = f"blob {len(content_bytes)}".encode("ascii") + b"\0"
    return hashlib.sha1(header + content_bytes).hexdigest()
