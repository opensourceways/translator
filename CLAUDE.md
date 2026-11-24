# CLAUDE.md

本文件为 AI 助手（及新接手开发者）提供 translator 仓库的全局上下文。

## 这是什么

translator：AtomGit 译稿自动提 PR 服务。调用方通过 HTTP API 提交译稿
权威仓（Computing_Ecosystem_Docs/Computing_Ecosystem_Docs-EN）中的文件
路径，服务读取文件内容后向对应社区目标仓自动 Fork/提交 PR（同仓同分支
合并为一个 PR），任务与逐仓结果落 SQLite 供查询。替代原 Jenkins 流水线
（translator-ci）。

曾用名 translator-ci → docsync → translator（2026-10 两次改名，env 前缀
相应为 TC_ → DS_ → TR_）。历史方案文档：
/home/tools/.trae/documents/translator-服务化改造计划.md

## 仓库结构

| 目录/文件 | 说明 |
| --- | --- |
| `translator/` | 引擎包：client.py（AtomGit API）、submit.py（提 PR）、localgit.py（克隆/读取文件）、index.py（目录层级与路径改写）、service.py（门面导出，仅 submit_repo）、secrets.py（凭据读取）。历史 CLI（cli.py/scan.py）已移除，仓库只保留 HTTP 服务形态 |
| `webapp/` | 常驻 API 服务（FastAPI）：api.py（提交/查询/PR 状态端点）、paths.py（路径解析与分组）、runner.py（Worker 执行编排）、queueing.py（FIFO 队列）、prstatus.py（PR 派生状态）、db.py（SQLite）、config.py（Settings） |
| `tests/` | pytest，全部 mock 不触网 |
| `deploy/k8s/` | K8s 清单（Namespace/ConfigMap/PVC/Deployment/Service/Ingress） |
| `Dockerfile` `docker-compose.yml` | 一体化 API 服务容器 |

## 常用命令

```bash
python3 -m pytest tests/ -q        # 全量测试（必须全绿再提交）
docker compose up -d --build       # 本地运行（改代码必须重建）
curl localhost:8000/healthz        # 探活 + 队列/任务计数
```

## 硬性约束（改动前必读）

- **单实例**：内存队列 + 全局唯一 Worker + 单 SQLite 连接。uvicorn 必须
  `workers=1`，部署不可多副本；K8s 用单副本 + Recreate + RWO PVC
- **SQLite 单写者**：改 db 文件名/路径前先停容器（`docker compose down`，
  确认容器已删再动 data/，否则 WAL 里最近的记录会丢）
- **调用方 token 永不落库**：`X-AtomGit-Token` 只存在于请求与 Worker
  内存；任何落库/落日志文本必须先过 `SecretMasker`
- **`AUTO_BRANCH_PREFIX = "translator/auto-"` 不可改**：已有 open PR 的
  head 分支去重依赖该前缀
- git 命令统一 `-c core.quotepath=false`（中文路径否则解析出垃圾目录名，
  见 commit 86c1746）
- **以非 root（uid/gid 1000）运行**：`/data` 属主不由镜像决定，K8s 靠
  `securityContext.fsGroup=1000`、compose 需一次性 `chown -R 1000:1000 ./data`
- **凭据不进进程环境**：应用直接读 `/vault/secrets/.env`（`translator/secrets.py`，
  文件优先、env 回退），启动脚本不再 `source` 成环境变量；该文件存在但不可读时
  启动即失败（fail-fast），不得静默以空 token 运行
- **服务级鉴权 fail-closed**：`TR_API_KEY` 缺失时 `create_app` 直接拒绝
  启动；所有 `/api/v1/*` 校验 `X-API-Key`（`hmac.compare_digest`）

## 配置约定

- env 前缀 `TR_*`（见 .env.example）：
  `TR_API_KEY`（必填）、`TR_DATA_DIR`、`TR_WAIT_TIMEOUT`、`TR_MAX_PATHS`、
  `TR_QUEUE_MAX`、`TR_SUBMISSION_RETENTION_DAYS`
- `ATOMGIT_TOKEN`：服务侧默认 token（必需），只用于克隆/读取权威仓与
  查询 PR 状态；目标仓 Fork/提交用调用方请求头的 token
- 凭据读取顺序：secrets 文件（`/vault/secrets/.env`，可用 `TR_SECRETS_FILE`
  覆盖）> 环境变量；生产由 Vault 注入文件，凭据不落 env
- 提交接口 `POST /api/v1/submissions`：`paths` 必填（上限 `TR_MAX_PATHS`）；
  `wait=true` 请求内等待至多 `TR_WAIT_TIMEOUT` 秒（完成 200、超时 202）；
  `request_id` 为幂等键（重复提交直接返回已有任务）；`dry_run=true` 只
  校验路径/大小（结果 `would_submit`）；队列满返回 429
- 路径解析（`webapp/paths.py`）：按组织层级（Ascend 4 段、CANN 默认 3 段、
  未登记组织按 org/产品/版本/仓库 兜底）识别版本段 → 目标 branch；
  版本段识别失败直接 422 拒绝，不猜测；同一 `(org, repo, branch)` 分组合并，
  同一目标仓内重复目标路径 422 拒绝
- 引擎目录约定与改写规则以 `translator/index.py` 为单一来源（webapp 直接
  复用 `_repo_depth` / `_target_rel_path`），不要在 webapp 里复制规则

## 任务状态口径

success / partial（部分仓库提交失败，非致命）/ failed（校验失败、全部
仓库失败或重启中断）；queued / running 为进行中。逐仓结果状态：
submitted / already_exists / noop / would_submit / failed。
排查失败：`GET /api/v1/submissions/{request_id}?include_log=true`
（日志尾部保留 200KB，已脱敏）。
