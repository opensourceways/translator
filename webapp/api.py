"""FastAPI Web 层：webhook 入口、Dashboard 与只读/管理 API。

对应服务化改造计划「API 端点」表：
- POST /webhook/gitcode：GitCode push 入口（?token= 鉴权，fail closed）；
  ref 过滤 ^(refs/heads/main|main|)$ 与 Jenkins regexpFilterExpression
  一致；所有到达事件（含被拒绝的）均落 push_events，接受的入队返回 202
- GET /healthz：无鉴权探活；busy 以库中是否存在 running run 判定
  （重启后也准确），不依赖 Worker 内存状态
- Dashboard 页面与 /api/* 只读 JSON：TR_DASH_TOKEN 未配置时放开，配置后
  要求 X-Dash-Token 头或 ?token= query
- POST /api/runs、/api/config/shadow、/api/settings/repos：
  X-Admin-Token 管理端点；admin_token 未配置时放开（内部工具，默认
  内网部署），配置后校验。命名 token 名称清单来自 Vault（只读，运维
  经 Vault 手动注入），服务不保存也不写入任何 token 值

lifespan：init_db → init_shadow（env 仅作种子，之后以库为准）→ 僵尸
running run 恢复为 interrupted → 停机补偿（最后一次成功 run 的
push_after 与来源仓远端默认分支 HEAD 比对，不一致入队一次全量扫；无
基线或比对失败都跳过，绝不影响启动）→ 启动 Worker/Poller。关停时协作
式停止两个线程并 join（Worker 正在执行的 run 会跑完）。
"""

import base64
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from translator import client, common, secrets

from webapp.config import Settings
from webapp.db import init_db, init_shadow, is_shadow, set_shadow
from webapp.prstatus import derive
from webapp.prsync import Poller
from webapp.queueing import CoalescingQueue, Trigger
from webapp.runner import Worker

log = logging.getLogger("webapp.api")

# webhook ref 过滤：与 Jenkins regexpFilterExpression 一致（空串也触发，
# 对应缺 ref 的兜底语义——sha 为空时队列语义自动全量扫描）
_REF_PATTERN = re.compile(r"^(refs/heads/main|main|)$")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# 内容安全策略：脚本仅允许同源 + 每请求 nonce（模板内联 <script> 均带该
# nonce）；样式保留 'unsafe-inline'（模板大量使用内联 style 属性与 <style>
# 块）。/docs、/redoc、/openapi.json 由 Swagger UI 从 CDN 加载资源，跳过 CSP
# 以免破坏文档页（其余安全响应头照常下发）。
_CSP = ("default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'")
_CSP_EXEMPT_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


def _group_prs(prs, statuses):
    """把 PR 列表与最新仓库状态合并为 社区(org) → (repo, branch) 分组结构。

    每组含该仓最新 run 状态（latest，无 run 记录为 None）与该仓全部 PR
    （number 倒序）；仅在 run_repos 中出现但没有任何 PR 的仓库也会成组
    （prs 为空，模板据此显示「未发现 PR」）。org 与组内仓库均按字母序
    稳定排序；org 附 open/merged/closed 三态计数（徽标用）。
    org/repo 分组大小写不敏感（GitCode 名称大小写不敏感，run_repos 与
    prs 两处来源可能大小写不一致），显示名取先见者的原始写法。

    Returns:
        list[dict]: [{org, counts, repos: [{org, repo, branch, latest,
        prs}]}]。
    """
    groups = {}
    names = {}

    def _key(org, repo, branch):
        k = ((org or "").lower(), (repo or "").lower(), branch)
        names.setdefault(k, (org, repo))
        return k

    for s in statuses:
        key = _key(s["org"], s["repo"], s["branch"])
        groups[key] = {
            "org": names[key][0], "repo": names[key][1],
            "branch": s["branch"], "latest": s, "prs": []}
    for pr in prs:
        key = _key(pr["org"], pr["repo"], pr.get("branch"))
        groups.setdefault(key, {"org": names[key][0], "repo": names[key][1],
                                "branch": key[2], "latest": None,
                                "prs": []})["prs"].append(pr)
    orgs = {}
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2] or "")):
        g = groups[key]
        g["prs"].sort(key=lambda p: p["number"], reverse=True)
        org = orgs.setdefault(key[0], {
            "org": names[key][0], "counts": {"open": 0, "merged": 0,
                                             "closed": 0},
            "repos": []})
        org["repos"].append(g)
        for pr in g["prs"]:
            if pr.get("state") in org["counts"]:
                org["counts"][pr["state"]] += 1
    return [orgs[name] for name in sorted(orgs)]


def _localtime(value):
    """库中 UTC ISO8601 时间戳转服务器本地时区显示。"""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _recover_missed_pushes(settings, db, queue):
    """停机补偿：最后一次成功 run 的 push_after 与来源仓远端 HEAD 比对。

    覆盖服务停机/重启窗口内丢失的 webhook（fire-and-forget）。不一致则
    入队一次全量扫描；无基线（全新库）、无 GITCODE_TOKEN 或查询远端失败
    （网络异常）都跳过——绝不影响启动。
    """
    token = secrets.get("GITCODE_TOKEN", "").strip()
    if not token:
        log.info("停机补偿跳过：GITCODE_TOKEN 未配置")
        return
    baseline = next(
        (r for r in db.list_runs(limit=50)
         if r["status"] in ("success", "partial") and r.get("push_after")),
        None)
    if baseline is None:
        log.info("停机补偿跳过：尚无成功 run 作为基线（全新库）")
        return
    try:
        owner, repo = common._source_repo()
        info = client.get_repo(owner, repo, token)
        default_branch = info.get("default_branch") or ""
        branches = client.list_branches(owner, repo, token)
        head_sha = next(
            ((b.get("commit") or {}).get("sha") for b in branches
             if b.get("name") == default_branch), None)
    except Exception as e:
        log.warning("停机补偿跳过：查询来源仓远端 HEAD 失败: %s", e)
        return
    if not head_sha:
        log.warning("停机补偿跳过：远端默认分支 %r 未找到", default_branch)
        return
    if head_sha == baseline["push_after"]:
        log.info("停机补偿：远端 HEAD 与基线一致，无需补偿")
        return
    log.info("停机补偿：基线 %s 落后远端 HEAD %s，入队一次全量扫描",
             baseline["push_after"], head_sha)
    queue.push(Trigger.manual(full_scan=True))


def create_app(settings=None):
    """构建 FastAPI 应用；线程与数据库生命周期由 lifespan 管理。

    settings 为 None 时从环境变量加载（uvicorn --factory 以零参调用）。
    app.state 上暴露 db / queue / worker / poller（测试与运维探针用）。
    """
    if settings is None:
        settings = Settings.from_env()
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    templates.env.filters["localtime"] = _localtime
    templates.env.autoescape = True  # 显式声明：所有变量输出默认 HTML 转义

    @asynccontextmanager
    async def lifespan(app):
        db = init_db(settings.db_path)
        init_shadow(db, settings.shadow)
        interrupted = db.interrupt_running_runs()
        if interrupted:
            log.info("启动恢复：%d 个僵尸 running run 标记为 interrupted",
                     interrupted)
        queue = CoalescingQueue()
        _recover_missed_pushes(settings, db, queue)
        worker = Worker(queue, settings, db)
        poller = Poller(db, settings)
        worker.start()
        poller.start()
        app.state.db = db
        app.state.queue = queue
        app.state.worker = worker
        app.state.poller = poller
        try:
            yield
        finally:
            worker.stop()
            poller.stop()
            worker.join()
            poller.join()
            db.close()

    app = FastAPI(title="translator", lifespan=lifespan)

    # ── 安全响应头 ───────────────────────────────────────────

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """为每个响应下发安全头；每请求生成 CSP nonce 供模板内联脚本引用。

        nonce 写入 request.state（scope["state"] 在中间件与路由间共享），
        _ctx 据此注入 csp_nonce 变量。
        """
        nonce = base64.b64encode(os.urandom(16)).decode()
        request.state.csp_nonce = nonce
        response = await call_next(request)
        if request.url.path not in _CSP_EXEMPT_PATHS:
            response.headers.setdefault(
                "Content-Security-Policy", _CSP.format(nonce=nonce))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # ── 鉴权依赖 ─────────────────────────────────────────────

    def require_dash(request: Request):
        """只读端点鉴权：dash_token 未配置时放开，否则校验头或 ?token=。"""
        if not settings.dash_token:
            return
        token = request.headers.get("x-dash-token", "")
        if not token:
            token = request.query_params.get("token", "")
        if not hmac.compare_digest(token, settings.dash_token):
            raise HTTPException(403, "dash token 无效或未提供")

    def require_admin(x_admin_token: str = Header(default="")):
        """管理端点鉴权：admin_token 未配置时放开（内部工具，默认内网
        部署），配置后校验 X-Admin-Token 头。"""
        if not settings.admin_token:
            return
        if not hmac.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(403, "admin token 无效")

    dash = [Depends(require_dash)]

    def _ctx(request, **kw):
        """模板公共上下文：shadow 角标 + dash token 是否启用（模板透传用）。"""
        return {"request": request,
                "dash_auth": bool(settings.dash_token),
                "csp_nonce": getattr(request.state, "csp_nonce", ""),
                "shadow": is_shadow(request.app.state.db), **kw}

    # ── webhook ──────────────────────────────────────────────

    @app.post("/webhook/gitcode")
    async def webhook_gitcode(request: Request,
                              token: str = Query(default="")):
        """GitCode push 入口：token 校验 → ref 过滤 → 落库 → 入队。

        body 非 JSON 或缺字段时按 Jenkins 兜底语义处理：ref 视为空串
        （命中过滤正则），sha 为空 → 队列语义自动全量扫描。
        """
        if not settings.webhook_token or not hmac.compare_digest(
                token, settings.webhook_token):
            raise HTTPException(403, "webhook token 无效或未配置")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        db = request.app.state.db
        ref = str(payload.get("ref") or "")
        before = str(payload.get("before") or "") or None
        after = str(payload.get("after") or "") or None
        accepted, reason = True, ""
        if not _REF_PATTERN.match(ref):
            accepted, reason = False, "ref 不在触发范围"
        elif after and set(after) == {"0"}:
            accepted, reason = False, "分支删除事件不触发"
        event_id = db.record_push_event(ref, before, after, accepted)
        if not accepted:
            log.info("webhook 事件 #%d 被拒绝：%s (ref=%r)",
                     event_id, reason, ref)
            return JSONResponse(
                {"accepted": False, "reason": reason, "event_id": event_id})
        queued = request.app.state.queue.push(
            Trigger.from_push(ref, before, after, event_id))
        return JSONResponse(
            {"accepted": True, "event_id": event_id, "queued": queued},
            status_code=202)

    # ── 探活 ─────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz(request: Request):
        db = request.app.state.db
        busy = any(r["status"] == "running" for r in db.list_runs(limit=10))
        return {"status": "ok", "shadow": is_shadow(db), "busy": busy,
                "pending": request.app.state.queue.pending_count(),
                "last_run_at": db.kv_get("last_run_at"),
                "last_poll_at": db.kv_get("last_poll_at")}

    # ── Dashboard 页面 ───────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, dependencies=dash)
    async def page_index(request: Request):
        db = request.app.state.db
        return templates.TemplateResponse(
            request, "index.html",
            _ctx(request, summary=db.summary(), runs=db.list_runs(limit=20)))

    @app.get("/runs", response_class=HTMLResponse, dependencies=dash)
    async def page_runs(request: Request):
        db = request.app.state.db
        return templates.TemplateResponse(
            request, "runs.html",
            _ctx(request, runs=db.list_runs(limit=100)))

    @app.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=dash)
    async def page_run_detail(request: Request, run_id: int):
        db = request.app.state.db
        run = db.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        return templates.TemplateResponse(
            request, "run_detail.html",
            _ctx(request, run=run, repos=db.get_run_repos(run_id)))

    @app.get("/prs", response_class=HTMLResponse, dependencies=dash)
    async def page_prs(request: Request):
        db = request.app.state.db
        prs = db.list_prs()
        for pr in prs:
            pr["derived"] = derive(pr)
        return templates.TemplateResponse(
            request, "prs.html",
            _ctx(request, summary=db.summary(),
                 orgs=_group_prs(prs, db.latest_repo_statuses())))

    # ── 只读 JSON ────────────────────────────────────────────

    @app.get("/api/summary", dependencies=dash)
    async def api_summary(request: Request):
        db = request.app.state.db
        return {**db.summary(), "shadow": is_shadow(db)}

    @app.get("/api/runs", dependencies=dash)
    async def api_runs(request: Request, limit: int = Query(default=50)):
        limit = min(max(limit, 1), 200)
        return {"runs": request.app.state.db.list_runs(limit=limit)}

    @app.get("/api/runs/{run_id}", dependencies=dash)
    async def api_run_detail(request: Request, run_id: int):
        db = request.app.state.db
        run = db.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        run = dict(run)
        run.pop("log", None)  # log 大字段走独立端点
        return {"run": run, "repos": db.get_run_repos(run_id)}

    @app.get("/api/runs/{run_id}/log", dependencies=dash)
    async def api_run_log(request: Request, run_id: int):
        run = request.app.state.db.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        return {"run_id": run_id, "log": run.get("log") or ""}

    @app.get("/api/prs", dependencies=dash)
    async def api_prs(request: Request, state: str = Query(default=""),
                      group_by: str = Query(default="")):
        """PR 列表：默认平铺（兼容旧结构）；?group_by=org 返回分组结构。

        每个 PR 对象附 derived 派生状态（key/text/color/guidance，
        由 prstatus.derive 从原始信号现算，调映射规则无需重新轮询）。
        """
        db = request.app.state.db
        prs = db.list_prs(state=state or None)
        for pr in prs:
            pr["derived"] = derive(pr)
        if group_by == "org":
            return {"orgs": _group_prs(prs, db.latest_repo_statuses())}
        return {"prs": prs}

    # ── 仓库配置（设置页）：一个条目配齐一个仓库的开关与专属 Token ──

    @app.get("/settings", response_class=HTMLResponse, dependencies=dash)
    async def page_settings(request: Request):
        db = request.app.state.db
        entries = db.list_repo_configs()
        known = {(e["org"], e["repo"]) for e in entries}
        suggestions = []
        seen = set()
        for r in db.distinct_repos():
            key = (r["org"].lower(), r["repo"].lower())
            if key not in known and key not in seen:
                seen.add(key)
                suggestions.append(r)
        token_names = secrets.list_token_names()
        return templates.TemplateResponse(
            request, "settings.html",
            _ctx(request, entries=entries,
                 suggestions=suggestions,
                 token_names=token_names,
                 admin_auth=bool(settings.admin_token)))

    @app.get("/api/settings/repos", dependencies=dash)
    async def api_list_repo_configs(request: Request):
        """仓库配置列表（token_name 为引用名，无敏感值）。"""
        return {"entries": request.app.state.db.list_repo_configs()}

    @app.post("/api/settings/repos",
              dependencies=[Depends(require_admin)])
    async def api_upsert_repo_config(request: Request):
        """新增/更新仓库配置：{org, repo, enabled?, token_name?}。

        enabled/token_name 缺省表示保持不变（新条目默认 enabled=True、
        不引用命名 token）；token_name 传空串表示清除引用（回退默认
        GITCODE_TOKEN）；非空的 token_name 必须是 Vault 中已配置的命名
        token 名称（TR_TOKEN_<名称>）。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        org = str(body.get("org") or "").strip()
        repo = str(body.get("repo") or "").strip()
        if not org or not repo:
            raise HTTPException(422, "body 需要非空字段 org 与 repo")
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise HTTPException(422, "enabled 必须是布尔值")
        token_name = body.get("token_name")
        if token_name is not None:
            token_name = str(token_name).strip().lower()
        db = request.app.state.db
        if token_name and token_name not in secrets.list_token_names():
            raise HTTPException(
                422, f"命名 Token「{token_name}」未在 Vault 配置，"
                     f"请联系运维注入 {secrets.TOKEN_PREFIX}"
                     f"{token_name.upper()}")
        entry_id = db.upsert_repo_config(org, repo, enabled=enabled,
                                         token_name=token_name)
        log.info("仓库配置 upsert：%s/%s enabled=%s token_name=%s",
                 org.lower(), repo.lower(), enabled,
                 "清除" if token_name == ""
                 else (token_name if token_name else "不变"))
        return {"id": entry_id, "org": org.lower(), "repo": repo.lower()}

    @app.post("/api/settings/repos/{entry_id}",
              dependencies=[Depends(require_admin)])
    async def api_toggle_repo_config(request: Request, entry_id: int):
        """切换条目开关：{enabled: bool}。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            raise HTTPException(422, "body 需要布尔字段 enabled")
        db = request.app.state.db
        if not db.set_repo_config_enabled(entry_id, enabled):
            raise HTTPException(404, "条目不存在")
        log.info("仓库配置 #%d 切换为 %s",
                 entry_id, "启用" if enabled else "禁用")
        return {"id": entry_id, "enabled": enabled}

    @app.delete("/api/settings/repos/{entry_id}",
                dependencies=[Depends(require_admin)])
    async def api_delete_repo_config(request: Request, entry_id: int):
        db = request.app.state.db
        if not db.delete_repo_config(entry_id):
            raise HTTPException(404, "条目不存在")
        log.info("仓库配置条目 #%d 已删除", entry_id)
        return {"id": entry_id, "deleted": True}

    # ── 命名 Token（只读：清单来自 Vault，新增/删除由运维手动操作） ──

    @app.get("/api/settings/tokens", dependencies=dash)
    async def api_list_named_tokens():
        """命名 token 名称清单（来自 Vault，仅名称，无任何 token 值）。"""
        return {"names": secrets.list_token_names()}

    # ── 管理端点 ─────────────────────────────────────────────

    @app.post("/api/runs", dependencies=[Depends(require_admin)])
    async def api_create_run(request: Request):
        """手动触发（Build Now 替代）：{full_scan, keyword} 入队，202。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        trigger = Trigger.manual(
            full_scan=bool(body.get("full_scan", True)),
            keyword=str(body.get("keyword") or ""))
        queued = request.app.state.queue.push(trigger)
        return JSONResponse({"queued": queued, "full_scan": trigger.full_scan,
                             "keyword": trigger.keyword}, status_code=202)

    @app.post("/api/runs/{run_id}/retry",
              dependencies=[Depends(require_admin)])
    async def api_retry_run(request: Request, run_id: int):
        """重提失败项：把该 run 中失败（submit_failed/error）的仓库重新入队。

        body 可带 {path} 精确重试单行；不带 path 则重试全部失败行。
        重试复用关键字扫描：单路径 keyword=path（子串匹配，可能带出同
        前缀的兄弟路径，多扫无害）；多个失败路径关键字互不覆盖，入队
        全量扫（与队列合并语义一致，宁多勿漏，未变更的仓库扫描后自然
        unchanged 不会重复提 PR）。新 run 按当前影子开关执行。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = str(body.get("path") or "").strip()
        db = request.app.state.db
        run = db.get_run(run_id)
        if run is None:
            raise HTTPException(404, "run 不存在")
        if run["status"] == "running":
            raise HTTPException(409, "run 仍在执行中，结束后再重试")
        failed = [r for r in db.get_run_repos(run_id)
                  if r["status"] in ("submit_failed", "error")]
        if path:
            if not any(r.get("path") == path for r in failed):
                raise HTTPException(
                    422, f"路径 {path!r} 不是该 run 的失败项，不能重试")
            trigger = Trigger.manual(keyword=path)
        else:
            if not failed:
                raise HTTPException(422, "该 run 没有可重试的失败项")
            paths = {r.get("path") for r in failed if r.get("path")}
            trigger = (Trigger.manual(keyword=paths.pop())
                       if len(paths) == 1
                       else Trigger.manual(full_scan=True))
        queued = request.app.state.queue.push(trigger)
        log.info("run #%d 重试入队：keyword=%r full_scan=%s（来源 %s）",
                 run_id, trigger.keyword, trigger.full_scan,
                 path or "全部失败项")
        return JSONResponse(
            {"queued": queued, "keyword": trigger.keyword,
             "full_scan": trigger.full_scan}, status_code=202)

    @app.post("/api/config/shadow", dependencies=[Depends(require_admin)])
    async def api_set_shadow(request: Request):
        """影子开关：{enabled: bool} 持久化到 config 表。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            raise HTTPException(422, "body 需要布尔字段 enabled")
        db = request.app.state.db
        set_shadow(db, enabled)
        log.info("影子模式切换为 %s", "开启" if enabled else "关闭")
        return {"shadow": is_shadow(db)}

    return app
