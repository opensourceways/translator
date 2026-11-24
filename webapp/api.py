"""FastAPI HTTP API：提交任务、任务查询与 PR 状态实时查询。

端点：
- GET  /livez                        存活探针（无鉴权，零依赖，K8s liveness 用）
- GET  /healthz                      就绪探活（无鉴权，含队列/SQLite 计数）
- POST /api/v1/submissions           提交目标仓文件路径，异步创建 PR
- GET  /api/v1/submissions/{id}      查询提交任务与逐仓库结果（含日志）
- GET  /api/v1/prs/status            实时查询 PR 状态（URL/编号/request_id）

鉴权：
- 服务级：所有 /api/v1 端点要求 ``X-API-Key``，值来自 TR_API_KEY
  （settings 或 Vault 注入文件）；未配置则服务拒绝启动（fail-closed）
- 调用方级：提交接口要求 ``X-AtomGit-Token``，仅用于目标仓 Fork/提交，
  只存在于请求与 Worker 内存中，永不落库

任务模型：提交落库后进入内存 FIFO 队列，由全局唯一 Worker 顺序执行；
同一 (org, repo, branch) 的路径合并为一个 PR。``paths`` 是目标仓坐标
（组织/仓库/分支/文件相对路径），权威仓内的实际存放路径由 Worker 在克隆
后精确解析。``dry_run=true`` 只校验路径与大小，不创建 PR。``wait=true``
在请求内等待任务结束（上限 settings.wait_timeout），超时返回 202 与
request_id 供后续轮询。
"""

import asyncio
import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from translator import client, secrets

from webapp.config import Settings
from webapp.db import init_db
from webapp.paths import group_paths
from webapp.prstatus import derive
from webapp.queueing import SubmissionQueue
from webapp.runner import SubmissionWorker

log = logging.getLogger("webapp.api")

# 调用方可选传入的幂等请求 ID：仅允许安全字符，长度受限
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# PR 链接解析：merge_requests 为现行路径，pulls 为兼容写法；
# atomgit.com 与 gitcode.com 是同一后端的两个访问域名，两种都接受
_PR_URL_RE = re.compile(
    r"^https?://(?:atomgit|gitcode)\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)"
    r"/(?:merge_requests|pulls)/(?P<number>\d+)(?:[/?#].*)?$")

# 目标仓分支链接（内容已在目标分支时的「已合入」标识，由 translator/submit.py
# 在 noop 且无历史 PR 时回传）
_BRANCH_URL_RE = re.compile(
    r"^https?://(?:atomgit|gitcode)\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)"
    r"/tree/(?P<branch>[^/]+)/?$")

# 等待任务结束的轮询间隔（秒）
_WAIT_POLL_INTERVAL = 0.2


def _map_state(pr):
    """把 AtomGit PR 响应映射为状态（merged/closed/open）。

    AtomGit（Gitee 系）PR 的 state 通常只有 open/closed，"已合并"看
    merged 字段为真或 merged_at 非空，两者任一成立即 merged。
    """
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "open"


def _extract_labels(pr):
    """从 PR 响应提取标签（名字+颜色）；无 labels 字段返回 None。"""
    if "labels" not in pr:
        return None
    return [{"name": l.get("name"), "color": l.get("color")}
            for l in pr.get("labels") or [] if isinstance(l, dict)]


def _pr_view(org, repo, number, pr):
    """把 AtomGit PR 详情整理为对外状态对象（含派生状态 derived）。"""
    assignees = None
    if "assignees" in pr:
        assignees = [a for a in pr.get("assignees") or []
                     if isinstance(a, dict)]
    view = {
        "org": org,
        "repo": repo,
        "number": number,
        "title": pr.get("title"),
        "url": (pr.get("html_url")
                or f"https://atomgit.com/{org}/{repo}/merge_requests/{number}"),
        "state": _map_state(pr),
        "mergeable": pr.get("mergeable"),
        "draft": pr.get("draft"),
        "labels": _extract_labels(pr),
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at"),
        "created_at": pr.get("created_at"),
        "review_total": (len(assignees) if assignees is not None else None),
        "review_accepted": (sum(1 for a in assignees if a.get("accept"))
                            if assignees is not None else None),
    }
    view["derived"] = derive(view)
    return view


def _branch_view(org, repo, branch, url):
    """把「内容已在目标分支」的分支链接整理成 PR 状态对象。

    不是真实 PR，但语义等价于已合入：内容已在目标分支上，调用方据此把
    任务推进到「已上线」。``synthetic=True`` 供排查时区分。
    """
    view = {
        "org": org,
        "repo": repo,
        "number": None,
        "title": f"内容已在目标分支 {branch}",
        "url": url,
        "state": "merged",
        "mergeable": None,
        "draft": None,
        "labels": None,
        "merged_at": None,
        "closed_at": None,
        "created_at": None,
        "review_total": None,
        "review_accepted": None,
        "synthetic": True,
        "branch": branch,
    }
    view["derived"] = derive(view)
    return view


def _fetch_pr_views(targets, token):
    """逐个查询 PR 状态（阻塞调用，由端点放进线程池执行）。

    Args:
        targets: [(org, repo, number)]，已去重。
        token: 读取 PR 使用的服务侧默认 token。

    Returns:
        (views, errors)：成功的状态对象列表与失败项列表。
    """
    views, errors = [], []
    for org, repo, number in targets:
        try:
            pr = client.get_pull_request(org, repo, number, token)
        except client.AtomGitError as exc:
            errors.append({"org": org, "repo": repo, "number": number,
                           "error": str(exc)})
            continue
        views.append(_pr_view(org, repo, number, pr))
    return views, errors


def _submission_payload(db, submission, include_log=False):
    """把提交任务与逐仓结果整理为对外 JSON。"""
    items = []
    for item in db.get_submission_items(submission["id"]):
        items.append({
            "org": item["org"],
            "repo": item["repo"],
            "branch": item["branch"],
            "status": item["status"],
            "stage": item["stage"],
            "source_paths": item["source_paths"],
            "target_paths": [f["target_path"] for f in item["files"]],
            "pr_number": item["pr_number"],
            "pr_url": item["pr_url"],
            "pr_title": item["pr_title"],
            "pr_existed": (None if item["pr_existed"] is None
                           else bool(item["pr_existed"])),
            "error": item["error"],
            "updated_at": item["updated_at"],
        })
    payload = {
        "request_id": submission["request_id"],
        "status": submission["status"],
        "dry_run": bool(submission["dry_run"]),
        "path_count": submission["path_count"],
        "created_at": submission["created_at"],
        "started_at": submission["started_at"],
        "finished_at": submission["finished_at"],
        "error": submission["error"],
        "items": items,
    }
    if include_log:
        payload["log"] = db.get_submission_log(submission["id"])
    return payload


def _validate_paths(raw_paths, max_paths):
    """校验并去重 paths 字段（保持调用方顺序）。

    Raises:
        HTTPException: 非数组、空数组、非字符串项、空串或数量超限。
    """
    if not isinstance(raw_paths, list) or not raw_paths:
        raise HTTPException(422, "paths 必须是非空字符串数组")
    paths, seen = [], set()
    for value in raw_paths:
        if not isinstance(value, str):
            raise HTTPException(422, f"paths 含非字符串项: {value!r}")
        path = value.strip()
        if not path:
            raise HTTPException(422, "paths 不能包含空字符串")
        if path not in seen:
            seen.add(path)
            paths.append(path)
    if len(paths) > max_paths:
        raise HTTPException(
            422, f"paths 数量 {len(paths)} 超过单次上限 {max_paths}")
    return paths


def _parse_pr_url(raw):
    """解析 AtomGit PR 链接为 (org, repo, number)。"""
    match = _PR_URL_RE.match(raw.strip())
    if not match:
        raise HTTPException(
            422, f"pr_url 不是合法的 AtomGit PR 链接: {raw!r}")
    return (match.group("org"), match.group("repo"),
            int(match.group("number")))


def create_app(settings=None):
    """构建 FastAPI 应用；数据库与 Worker 生命周期由 lifespan 管理。

    settings 为 None 时从环境变量加载（uvicorn --factory 以零参调用）。
    app.state 上暴露 db / queue / worker（测试与运维探针用）。
    """
    if settings is None:
        settings = Settings.from_env()
    # API Key 允许放 Vault 注入文件（凭据不进进程环境），环境变量兜底
    api_key = (settings.api_key or "").strip() or \
        secrets.get("TR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "未配置服务级 API Key（TR_API_KEY），拒绝以无鉴权方式启动")

    @asynccontextmanager
    async def lifespan(app):
        db = init_db(settings.db_path)
        interrupted = db.fail_unfinished_submissions(
            "服务重启，排队/执行中的任务已中断，请重新提交")
        if interrupted:
            log.info("启动恢复：%d 个未完成任务标记为 failed", interrupted)
        cleaned = db.cleanup_old_submissions(settings.retention_days)
        if cleaned["submissions"]:
            log.info("启动清理：删除 %d 个超过 %d 天的提交任务",
                     cleaned["submissions"], settings.retention_days)
        queue = SubmissionQueue(maxsize=settings.queue_max)
        worker = SubmissionWorker(queue, settings, db)
        worker.start()
        app.state.db = db
        app.state.queue = queue
        app.state.worker = worker
        try:
            yield
        finally:
            worker.stop()
            worker.join()
            db.close()

    app = FastAPI(title="translator", lifespan=lifespan)

    # ── 鉴权 ─────────────────────────────────────────────────

    def require_api_key(x_api_key: str = Header(default="",
                                                alias="X-API-Key")):
        """服务级鉴权：校验 X-API-Key 头。"""
        if not hmac.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "X-API-Key 无效或未提供")

    # ── 探活 ─────────────────────────────────────────────────

    @app.get("/livez")
    async def livez():
        """存活探针：只证明进程能响应请求。

        刻意不碰 SQLite、不加锁、不读文件——liveness 只回答「要不要重启」，
        数据库故障时应当摘流量（readiness）而不是重启容器，否则会把
        「存储坏了」放大成「反复重启」。分层自检见 ``python -m webapp.doctor``。
        """
        return {"status": "alive"}

    @app.get("/healthz")
    async def healthz(request: Request):
        db = request.app.state.db
        return {
            "status": "ok",
            "queue": request.app.state.queue.pending_count(),
            "pending": db.pending_count(),
            "running": db.running_count(),
        }

    # ── 提交任务 ─────────────────────────────────────────────

    @app.post("/api/v1/submissions",
              dependencies=[Depends(require_api_key)])
    async def create_submission(
            request: Request,
            x_atomgit_token: str = Header(default="",
                                          alias="X-AtomGit-Token")):
        """提交目标仓文件路径，异步创建 PR。

        请求体：{paths: [str], wait?: bool, wait_timeout?: int,
        dry_run?: bool, request_id?: str}。
        request_id 可选：重复提交同一 request_id 直接返回已有任务。
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(422, "请求体必须是 JSON 对象")
        if not isinstance(body, dict):
            raise HTTPException(422, "请求体必须是 JSON 对象")

        token = (x_atomgit_token or "").strip()
        if not token:
            raise HTTPException(
                422, "缺少 X-AtomGit-Token 请求头（调用方 AtomGit token）")
        if any(ch.isspace() for ch in token):
            raise HTTPException(422, "X-AtomGit-Token 不能包含空白字符")

        paths = _validate_paths(body.get("paths"), settings.max_paths)
        try:
            groups = group_paths(paths)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise HTTPException(422, "dry_run 必须是布尔值")
        wait = body.get("wait", False)
        if not isinstance(wait, bool):
            raise HTTPException(422, "wait 必须是布尔值")
        timeout = body.get("wait_timeout")
        if timeout is None:
            timeout = settings.wait_timeout
        elif isinstance(timeout, bool) or not isinstance(timeout, int):
            raise HTTPException(422, "wait_timeout 必须是整数秒")
        timeout = min(max(timeout, 1), settings.wait_timeout)

        request_id = str(body.get("request_id") or "").strip()
        if request_id and not _REQUEST_ID_RE.match(request_id):
            raise HTTPException(
                422, "request_id 仅允许字母/数字/点/下划线/连字符，长度 1-64")
        db = request.app.state.db
        if request_id:
            existing = db.get_submission_by_request_id(request_id)
            if existing is not None:
                return _submission_payload(db, existing)
        else:
            request_id = uuid.uuid4().hex

        queue = request.app.state.queue
        if queue.pending_count() >= settings.queue_max:
            raise HTTPException(429, "提交队列已满，请稍后重试")

        submission_id = db.create_submission(
            request_id, paths, groups, dry_run=dry_run)
        if not queue.push(submission_id, token):
            error = "提交队列已满，任务被拒绝"
            db.fail_queued_items(submission_id, error)
            db.finish_submission(submission_id, "failed", error=error)
            raise HTTPException(429, error)
        log.info("提交任务 %s 入队：%d 个路径 / %d 个仓库",
                 request_id, len(paths), len(groups))

        if wait:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                submission = db.get_submission(submission_id)
                if db.is_terminal(submission["status"]):
                    return JSONResponse(
                        _submission_payload(db, submission), status_code=200)
                await asyncio.sleep(_WAIT_POLL_INTERVAL)
        submission = db.get_submission(submission_id)
        return JSONResponse(
            _submission_payload(db, submission), status_code=202)

    @app.get("/api/v1/submissions/{request_id}",
             dependencies=[Depends(require_api_key)])
    async def get_submission(
            request: Request, request_id: str,
            include_log: bool = Query(default=False)):
        """查询提交任务状态与逐仓库结果；include_log=true 返回脱敏日志。"""
        db = request.app.state.db
        submission = db.get_submission_by_request_id(request_id)
        if submission is None:
            raise HTTPException(404, "提交任务不存在")
        return _submission_payload(db, submission, include_log=include_log)

    # ── PR 状态实时查询 ──────────────────────────────────────

    @app.get("/api/v1/prs/status", dependencies=[Depends(require_api_key)])
    async def pr_status(
            request: Request,
            pr_url: str = Query(default=""),
            org: str = Query(default=""),
            repo: str = Query(default=""),
            number: int = Query(default=0),
            request_id: str = Query(default="")):
        """实时查询 PR 状态（不走缓存，每次直查 AtomGit）。

        三种用法互斥：pr_url；org+repo+number；request_id（该任务下
        全部已建 PR）。返回对象含原始信号与 derived 派生状态。

        特殊：``pr_url`` 若是目标仓分支链接（``/tree/<branch>``，由
        noop 提交回传，表示内容已在目标分支），不查 AtomGit，直接返回
        ``state=merged`` 的合成状态。
        """
        if pr_url:
            if org or repo or number or request_id:
                raise HTTPException(
                    422, "pr_url 与 org/repo/number/request_id 不能同时使用")
            branch_match = _BRANCH_URL_RE.match(pr_url.strip())
            if branch_match:
                return {"prs": [_branch_view(
                    branch_match.group("org"), branch_match.group("repo"),
                    branch_match.group("branch"), pr_url.strip())],
                    "errors": []}

        token = secrets.get("ATOMGIT_TOKEN", "").strip()
        if not token:
            raise HTTPException(503, "服务未配置 ATOMGIT_TOKEN，无法查询 PR")
        db = request.app.state.db

        submission = None
        targets = []
        if pr_url:
            targets.append(_parse_pr_url(pr_url))
        elif request_id:
            if org or repo or number:
                raise HTTPException(
                    422, "request_id 与 org/repo/number 不能同时使用")
            submission = db.get_submission_by_request_id(request_id)
            if submission is None:
                raise HTTPException(404, "提交任务不存在")
            seen = set()
            for item in db.get_items_with_pr(submission["id"]):
                key = (item["org"], item["repo"], item["pr_number"])
                if key not in seen:
                    seen.add(key)
                    targets.append(key)
        else:
            if not org or not repo or number <= 0:
                raise HTTPException(
                    422, "需提供 pr_url，或 org/repo/number，或 request_id")
            if "/" in org or "/" in repo:
                raise HTTPException(422, "org 与 repo 不能包含 /")
            targets.append((org, repo, number))

        if not targets:
            payload = {"prs": [], "errors": []}
            if submission is not None:
                payload["request_id"] = submission["request_id"]
                payload["status"] = submission["status"]
            return payload

        views, errors = await asyncio.to_thread(
            _fetch_pr_views, targets, token)
        if submission is None and errors:
            # 单 PR 查询：失败直接映射 AtomGit 错误（404 未找到 / 502 上游）
            status = 404 if "404" in errors[0]["error"] else 502
            raise HTTPException(
                status, f"查询 PR 状态失败: {errors[0]['error']}")
        payload = {"prs": views, "errors": errors}
        if submission is not None:
            payload["request_id"] = submission["request_id"]
            payload["status"] = submission["status"]
        return payload

    return app
