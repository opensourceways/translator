"""AtomGit REST API v5 客户端。

封装对 AtomGit 平台所有需要的 API 调用，包括仓库信息查询、分支管理、
文件读写、Fork 同步和 Pull Request 操作。所有函数通过 Bearer Token 鉴权，
请求失败时统一抛出 AtomGitError。
"""

import base64
import threading
import time
from collections import deque
from urllib.parse import quote

import requests

BASE_URL = "https://api.atomgit.com/api/v5"
MAX_RETRIES = 4
RETRY_DELAY = 5
DEFAULT_TIMEOUT = 30          # 单次请求超时（秒），防止连接挂死
SLOW_TIMEOUT = 120            # 慢操作超时（秒）：创建大 PR 时服务端需逐文件
                              # 计算变更，文件量大时响应远超普通接口

# 进程级限流：AtomGit 单账号 50 次/分钟，而默认 Token 与规则 Token 属于
# 同一账号，全部请求共享同一配额。把任意 60 秒窗口内的请求数压在上限
# 之下（留余量），从源头避免触发 429；指数退避只兜底窗口重置前的个别
# 突发。上限按「每账号」而非「每 Token」计，因此全局共享一个计数器。
RATE_LIMIT_MAX = 45
RATE_LIMIT_WINDOW = 60.0
_rate_lock = threading.Lock()
_recent_requests = deque()


def _throttle():
    """限流：保证任意 RATE_LIMIT_WINDOW 秒窗口内最多发起 RATE_LIMIT_MAX 次请求。

    需等待时每次只睡一小段后重新检查，保证锁内操作极短、不阻塞其他线程。
    """
    while True:
        with _rate_lock:
            now = time.monotonic()
            while (_recent_requests and
                    now - _recent_requests[0] >= RATE_LIMIT_WINDOW):
                _recent_requests.popleft()
            if len(_recent_requests) < RATE_LIMIT_MAX:
                _recent_requests.append(now)
                return
            wait = _recent_requests[0] + RATE_LIMIT_WINDOW - now
        time.sleep(max(wait, 0.01))


def _reset_rate_limit():
    """清空限流计数（仅测试用）。"""
    with _rate_lock:
        _recent_requests.clear()


class AtomGitError(Exception):
    """AtomGit API 返回的错误响应。"""

    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(f"[{status_code}] {message}")


def _headers(token):
    """构造 Bearer Token 鉴权的请求头。"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _retry_delay(r, attempt):
    """计算重试前等待时长。

    429 限流按分钟窗口计数：优先按服务端 Retry-After 头等待，否则
    指数退避（5/10/20/40 秒，跨过一分钟窗口后大概率已重置）；其余
    （GET 5xx 网关错误）保持固定短间隔即可。
    """
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), 60)
        return RETRY_DELAY * (2 ** attempt)
    return RETRY_DELAY


def _do_request(method, url, token, timeout=None, **kwargs):
    """发起 HTTP 请求，遇到 429 / GET 5xx 自动等待后重试（最多 {MAX_RETRIES} 次）。

    每次请求前先过进程级限流（_throttle），从源头把请求数控制在单账号
    限流上限之下；429 按 Retry-After/指数退避等待以跨过限流窗口，GET 5xx
    幂等，短暂等待后重试更稳。所有请求统一携带默认超时（可用 timeout
    覆盖），避免连接挂死。
    """
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = requests.request(method, url, headers=_headers(token),
                                 timeout=timeout or DEFAULT_TIMEOUT, **kwargs)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            # 网络层异常（读超时/断连）多为瞬时故障：等待后重试。写操作若
            # 首次实际已在服务端成功，重试会收到 409，由上层按幂等口径
            # 处理。重试耗尽仍失败时包装为 AtomGitError 抛出，让单仓
            # 失败不中断整个批次（此前裸 requests 异常会直接炸掉全程）。
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
                continue
            raise AtomGitError(0, f"网络错误（已重试 {MAX_RETRIES} 次）: {e}") from e
        if (r.status_code == 429 or
                (method == "GET" and 500 <= r.status_code < 600)) \
                and attempt < MAX_RETRIES - 1:
            time.sleep(_retry_delay(r, attempt))
            continue
        if not r.ok:
            raise AtomGitError(r.status_code, _extract_error(r))
        return r.json()


def get_repo(owner, repo, token):
    """获取仓库基本信息（含默认分支）。"""
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}", token)


def _paginate(url, token, params, per_page=100):
    """按 page 参数分页拉取全量数组（分支/PR 等列表接口）。

    平台单页上限 100，列表可能超出单页；逐页取到空或不足一整页为止，
    避免列表被截断导致漏判（如分支不存在、open PR 去重失效）。
    兜底：个别接口（如 PR files）忽略 page 参数，每页都返回同一份全量
    ——返回条数超过 per_page、或与上一页首条相同即终止，否则会以
    限流速率无限打请求（#80 卡死一小时无任何日志）。
    """
    items = []
    page = 1
    prev_first = None
    while True:
        batch = _do_request("GET", url, token,
                            params={**params, "page": page,
                                    "per_page": per_page})
        if not isinstance(batch, list):
            raise AtomGitError(200, f"分页响应不是数组: {url}")
        if not batch:
            break
        if len(batch) > per_page:
            # 服务端忽略 per_page、一次性返回全量：该批即全集
            return batch
        first = batch[0]
        if first == prev_first:
            # page 参数被忽略，每页内容相同：避免重复计入与死循环
            break
        prev_first = first
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


def list_branches(owner, repo, token, per_page=100):
    """列出仓库所有分支（自动分页拉取全量）。"""
    return _paginate(f"{BASE_URL}/repos/{owner}/{repo}/branches", token,
                     {}, per_page=per_page)


def get_contents(owner, repo, path, ref, token):
    """查询指定路径上文件是否存在，存在则返回文件元信息（含 sha）；
    返回 None 表示文件不存在（404）。"""
    try:
        return _do_request("GET",
                           f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                           token, params={"ref": ref})
    except AtomGitError as e:
        if e.status_code == 404:
            return None
        raise


def list_dir(owner, repo, path, ref, token):
    """列出指定目录下的条目（文件和子目录）。

    contents API 对目录路径返回条目数组，每个条目含
    name / path / type（"file" 或 "dir"）等字段。
    根目录传空字符串即可。
    """
    url = f"{BASE_URL}/repos/{owner}/{repo}/contents"
    if path:
        url += "/" + quote(path, safe="/")
    result = _do_request("GET", url, token, params={"ref": ref})
    if not isinstance(result, list):
        raise AtomGitError(200, f"路径不是目录: {path or '/'}")
    return result


def get_file_bytes(owner, repo, path, ref, token):
    """读取指定文件的原始字节内容（base64 解码，不假定文本编码）。

    兼容二进制文件（如图片）；文本文件同样适用。文件不存在时抛出
    AtomGitError(404)。
    """
    result = _do_request("GET",
                         f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                         token, params={"ref": ref})
    content_b64 = result.get("content", "")
    # API 返回的 base64 可能按行折叠，需先去除空白字符
    return base64.b64decode("".join(content_b64.split()))


def get_file_text(owner, repo, path, ref, token):
    """读取指定文件的文本内容（UTF-8 解码，适用于 Markdown/JSON 等文本）。

    对二进制文件调用会抛 UnicodeDecodeError，读取原始字节请用 get_file_bytes。
    """
    return get_file_bytes(owner, repo, path, ref, token).decode("utf-8")


def list_tree(owner, repo, ref, token):
    """递归列出仓库指定 ref 下全部文件路径。

    .. deprecated::
        逐目录 contents 遍历目录数多时需上百次请求，触发限流后
        单仓清点可达 20 分钟（构建 #61 实测）。仅保留给低频诊断使用。
    """
    paths = set()
    stack = [""]
    while stack:
        path = stack.pop()
        for entry in list_dir(owner, repo, path, ref, token):
            if entry.get("type") == "dir":
                stack.append(entry["path"])
            elif entry.get("type") == "file":
                paths.add(entry["path"])
    return paths


def get_user(token):
    """获取当前 token 对应的用户信息，用于确定 fork 目标账号。"""
    return _do_request("GET", f"{BASE_URL}/user", token)


def list_repo_forks(owner, repo, token, page=1, per_page=100):
    """列出目标仓库的全部 Fork（用于按 owner 匹配当前用户的已有 Fork）。

    Fork 后的仓库路径可能与原仓库名不同，不能按路径猜测；目标仓库的
    Fork 列表每条都带 owner 信息，匹配更可靠。
    """
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}/forks", token,
                       params={"page": page, "per_page": per_page})


def fork_repo(owner, repo, token):
    """将目标仓库 fork 到当前用户账号下。"""
    return _do_request("POST", f"{BASE_URL}/repos/{owner}/{repo}/forks", token)


def sync_repo(owner, repo, token, branch=None):
    """触发 Fork 仓库与源仓库同步（异步操作，用 get_sync_status 查询进度）。

    仅适用于 Fork 仓库；branch 指定要拉取更新的分支（需放在 JSON 请求
    体中），不传则更新所有分支。Fork 不会自动跟进上游新增/更新的分支，
    建分支前需先同步，否则上游的 commit sha / 新分支名在 Fork 上可能
    不存在。
    """
    payload = {"branch": branch} if branch else None
    return _do_request("PUT", f"{BASE_URL}/repos/{owner}/{repo}/sync_repo",
                       token, json=payload)


def get_sync_status(owner, repo, token, branch=None):
    """查询 Fork 仓同步状态，返回 {repo_sync_result, repo_sync_message}。"""
    params = {"branch": branch} if branch else None
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}/sync_repo",
                       token, params=params)


def list_pull_requests(owner, repo, token, state="open", per_page=100):
    """列出仓库的 Pull Request（默认仅 open），自动分页拉取全量。

    注意：AtomGit 服务端不支持 head/base 过滤参数（带了会返回空
    列表），调用方需拉全量 open PR 后自行按 head/base 过滤。
    """
    return _paginate(f"{BASE_URL}/repos/{owner}/{repo}/pulls", token,
                     {"state": state}, per_page=per_page)


def get_pull_request(owner, repo, number, token):
    """查询单个 Pull Request 的详情（state/merged_at/mergeable 等）。

    供后台轮询 PR 状态用：确认是否已合入或关闭、以及合并结果。
    """
    return _do_request("GET",
                       f"{BASE_URL}/repos/{owner}/{repo}/pulls/{number}", token)


def create_pr(owner, repo, title, head, base, body, token, fork_path=None):
    """创建 Pull Request。

    head 格式为 "用户名:分支名"；跨仓 PR 必须传 fork_path
    （Fork 仓库的完整路径 owner/repo，Fork 后的仓库名可能与原仓库不同）。
    """
    payload = {
        "title": title,
        "head": head,
        "base": base,
        "body": body,
        "prune_source_branch": True,
    }
    if fork_path:
        payload["fork_path"] = fork_path
    return _do_request("POST", f"{BASE_URL}/repos/{owner}/{repo}/pulls", token,
                       timeout=SLOW_TIMEOUT, json=payload)


def _extract_error(r):
    """从响应体中提取错误消息：优先取 message/error 字段，否则回退到原文。"""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or r.text
        return r.text
    except Exception:
        return r.text
