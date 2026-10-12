"""service 各子模块共享的常量、来源仓库解析与通用工具。

index / scan / submit 都从本模块获取共享定义，避免子模块之间相互引用
造成循环依赖：
- 仓库与路径常量（SOURCE_ORG / SOURCE_REPO、MAX_TOTAL_SIZE、
  AUTO_BRANCH_PREFIX）
- _source_repo：译稿来源仓库的解析（支持 ATOMGIT_SOURCE_REPO 覆盖）
- _git_blob_sha：git blob sha 计算（免下载内容对比）
"""

import hashlib
import re

from translator import secrets

# 译稿权威来源仓库（默认）；可用 ATOMGIT_SOURCE_REPO 环境变量覆盖为
# 自己的 Fork（如 "lei0308/Computing_Ecosystem_Docs-EN"），便于在
# 没有权威仓库管理权限时测试（Webhook 配在 Fork 上）
SOURCE_ORG = "Computing_Ecosystem_Docs"
SOURCE_REPO = "Computing_Ecosystem_Docs-EN"
GIT_HOST = "https://atomgit.com"  # git 协议仓库地址（API 用 client.BASE_URL）

MAX_FILE_SIZE = 50 * 1024 * 1024         # 单文件大小上限 50 MB（防误传大文件）
MAX_TOTAL_SIZE = 100 * 1024 * 1024       # 单次上传累计文件大小上限 100 MB
AUTO_BRANCH_PREFIX = "translator/auto-"  # 自动提交的固定分支名前缀


def _source_repo():
    """返回译稿来源仓库 (owner, repo)，优先取 ATOMGIT_SOURCE_REPO 环境变量。

    对环境变量的写法做宽容解析：允许粘贴完整 URL
    （https://atomgit.com/owner/repo）或带 .git 后缀，只取最后两段。
    """
    full = secrets.get("ATOMGIT_SOURCE_REPO", "").strip()
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
