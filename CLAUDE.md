# CLAUDE.md

本文件为 AI 助手（及新接手开发者）提供 translator 仓库的全局上下文。

## 这是什么

translator：AtomGit 译稿自动提 PR 服务。监听译稿权威仓
（Computing_Ecosystem_Docs/Computing_Ecosystem_Docs-EN）的 push webhook，
扫描变更后向各社区目标仓自动 Fork/提交 PR，并提供 Dashboard 监控 PR 状态。
替代原 Jenkins 流水线（translator-ci）。

曾用名 translator-ci → docsync → translator（2026-10 两次改名，env 前缀
相应为 TC_ → DS_ → TR_）。历史方案文档：
/home/tools/.trae/documents/translator-服务化改造计划.md

## 仓库结构

| 目录/文件 | 说明 |
| --- | --- |
| `translator/` | 引擎包：client.py（AtomGit API）、scan.py（变更扫描）、submit.py（提 PR）、localgit.py（本地克隆索引）、service.py（门面导出）、secrets.py（凭据读取/命名 token 枚举）、cli.py |
| `webapp/` | 服务层（FastAPI）：api.py（webhook/Dashboard/管理端点）、runner.py（Worker 执行编排）、queueing.py（push 合并队列）、prsync.py（PR 状态轮询）、prstatus.py（PR 派生状态）、db.py（SQLite）、token_router.py（命名 token 路由）、templates/（Jinja2 页面） |
| `tests/` | pytest，全部 mock 不触网 |
| `deploy/k8s/` | K8s 清单（Namespace/ConfigMap/PVC/Deployment/Service；Secret 仅模板） |
| `Dockerfile` `docker-compose.yml` | 一体化容器（前后端同进程，无独立前端） |
| `Jenkinsfile` | 历史遗留（translator-ci 时代），仅存档参考，勿用于新部署 |

## 常用命令

```bash
python3 -m pytest tests/ -q        # 全量测试（必须全绿再提交）
docker compose up -d --build       # 本地运行（模板打进镜像，改代码/模板必须重建）
curl localhost:8000/healthz        # 探活 + 影子状态 + 队列深度
```

## 硬性约束（改动前必读）

- **单实例**：内存队列 + 全局唯一 Worker + 单 SQLite 连接。uvicorn 必须
  `workers=1`，部署不可多副本；K8s 用单副本 + Recreate + RWO PVC
- **SQLite 单写者**：改 db 文件名/路径前先停容器（`docker compose down`，
  确认容器已删再动 data/，否则 WAL 里最近的 run 记录会丢）
- **Secret 脱敏**：任何落库/落日志文本必须先过 `SecretMasker`；
  clone 失败的 AtomGitError 会携带含 token 的 URL，是历史事故点
- **`AUTO_BRANCH_PREFIX = "translator/auto-"` 不可改**：已有 open PR 的
  head 分支去重依赖该前缀
- git 命令统一 `-c core.quotepath=false`（中文路径否则解析出垃圾目录名，
  见 commit 86c1746）
- **以非 root（uid/gid 1000）运行**：`/data` 属主不由镜像决定，K8s 靠
  `securityContext.fsGroup=1000`、compose 需一次性 `chown -R 1000:1000 ./data`
- **凭据不进进程环境**：应用直接读 `/vault/secrets/.env`（`translator/secrets.py`，
  文件优先、env 回退），启动脚本不再 `source` 成环境变量；该文件存在但不可读时
  启动即失败（fail-fast），不得静默以空 token 运行
- **模板内联 `<script>` 必须带 `nonce="{{ csp_nonce }}"`**：CSP 脚本源仅同源 +
  每请求 nonce，漏加 nonce 的脚本会被浏览器拦截（见 `webapp/api.py`
  的 `security_headers` 中间件与 `_CSP`）

## 配置约定

- env 前缀 `TR_*`（见 .env）；`ATOMGIT_TOKEN` 为默认 token（必需）
- 凭据读取顺序：secrets 文件（`/vault/secrets/.env`，可用 `TR_SECRETS_FILE`
  覆盖）> 环境变量；生产由 Vault 注入文件，凭据不落 env
- 影子模式：`TR_SHADOW` 仅作首启种子，运行时真值在库中
  （`POST /api/config/shadow` 或页面切换）；影子=全流程照跑但不建 PR
- Token 路由：仓库配置引用命名 token 名称（设置页维护）→ Vault 的
  `TR_TOKEN_<名称大写>`；名称缺失/非法时该仓直接失败（stage=token_missing），
  不回退默认 ATOMGIT_TOKEN；CLI 模式统一用默认 ATOMGIT_TOKEN
- 仓库自动提 PR 门控：设置页 repo_configs，无条目/已禁用默认拒绝
  （noop，stage=auto_pr_off）
- 管理端点：`TR_ADMIN_TOKEN` 空则放开（内网默认）；前端 localStorage
  键 `tc_admin_token` 与设置页共用
- 触发方式：`POST /api/runs` 支持 `{full_scan|keyword}`（全量/关键字扫）
  与 `{commit}`（单 commit 变更集，只处理该 commit 自身引入的变更，内容
  取自该 commit 的树、diff 区间为 `父..该 commit`）；commit 可写完整 40
  位或最短 4 位缩写，经权威仓 commits 端点解析为全量并做可达性校验，
  否则 422 拒绝（不回退全量），与另外两种互斥

## run 状态口径

success / partial（扫描有 errors 或提交有 failed，非致命）/ failed（致命）/
interrupted（重启僵尸恢复）。排查 partial：run 详情页仓库结果表
error/submit_failed 行 + 完整日志（`/api/runs/{id}/log`，尾部 200KB）。
失败项可在 run 详情页一键重试（`POST /api/runs/{id}/retry`）。
