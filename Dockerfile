FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY docsync ./docsync
COPY webapp ./webapp
RUN pip install --no-cache-dir ".[services]"

# 单实例硬性约束：内存队列 + 全局唯一 Worker + 单 SQLite 连接，
# workers 必须为 1 且不可多副本部署
CMD ["uvicorn", "webapp.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
