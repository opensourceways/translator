# 基础镜像用 openEuler（与 hotopic 等组织内服务一致，上线要求）；
# openEuler 24.03 自带 Python 3.11，满足 requires-python >= 3.10
FROM openeuler/openeuler:24.03

RUN dnf install -y --setopt=install_weak_deps=False \
        git python3 python3-pip ca-certificates \
    && dnf clean all

WORKDIR /app
COPY pyproject.toml README.md ./
COPY translator ./translator
COPY webapp ./webapp
RUN pip3 install --no-cache-dir ".[services]"

# 单实例硬性约束：内存队列 + 全局唯一 Worker + 单 SQLite 连接，
# workers 必须为 1 且不可多副本部署
# 启动前加载 /vault/secrets/.env（Vault Agent Injector 注入的敏感变量，
# 参照 jenkins-log-viewer 先例）；文件不存在（本地/compose）时直接跳过
CMD ["sh", "-c", "set -a; [ ! -f /vault/secrets/.env ] || . /vault/secrets/.env; set +a; exec uvicorn webapp.api:create_app --factory --host 0.0.0.0 --port 8000 --workers 1"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
