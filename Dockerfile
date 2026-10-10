# 基础镜像用 openEuler（与 hotopic 等组织内服务一致，上线要求）；
# openEuler 24.03 自带 Python 3.11，满足 requires-python >= 3.10
FROM openeuler/openeuler:24.03

RUN dnf install -y --setopt=install_weak_deps=False \
        git python3 python3-pip ca-certificates \
    && dnf clean all \
    && groupadd -g 1000 translator \
    && useradd -m -u 1000 -g translator -s /sbin/nologin translator \
    && mkdir -p /data && chown translator:translator /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY translator ./translator
COPY webapp ./webapp
RUN pip3 install --no-cache-dir ".[services]"

ENV HOME=/home/translator

# 单实例硬性约束：内存队列 + 全局唯一 Worker + 单 SQLite 连接，
# workers 必须为 1 且不可多副本部署
# 启动前加载 /vault/secrets/.env（Vault Agent Injector 注入的敏感变量，
# 参照 jenkins-log-viewer 先例）；文件不存在（本地/compose）时直接跳过。
# set -e + 显式判断：文件存在但不可读时立即非零退出，避免静默以空 token 启动
CMD ["sh", "-c", "set -e; if [ -f /vault/secrets/.env ]; then set -a; . /vault/secrets/.env; set +a; fi; exec uvicorn webapp.api:create_app --factory --host 0.0.0.0 --port 8000 --workers 1"]

# 非 root 运行（uid/gid 1000）：镜像内 USER 只决定进程身份，/app 仅需读；
# /data 属主由 K8s fsGroup 或 compose 侧一次性 chown 保证（镜像无法决定挂载点属主）
USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
