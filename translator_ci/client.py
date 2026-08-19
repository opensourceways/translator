"""GitCode REST API v5 客户端。

封装对 GitCode 平台所有需要的 API 调用，包括仓库信息查询、分支管理、
文件读写、Fork 同步和 Pull Request 操作。所有函数通过 Bearer Token 鉴权，
请求失败时统一抛出 GitCodeError。
"""

import base64
import time
from urllib.parse import quote

import requests

BASE_URL = "https://api.gitcode.com/api/v5"
MAX_RETRIES = 3
RETRY_DELAY = 5


class GitCodeError(Exception):
    """GitCode API 返回的错误响应。"""

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


def _do_request(method, url, token, **kwargs):
    """发起 HTTP 请求，遇到 429 自动等待后重试（最多 {MAX_RETRIES} 次）。"""
    for attempt in range(MAX_RETRIES):
        r = requests.request(method, url, headers=_headers(token), **kwargs)
        if r.status_code == 429 and attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_DELAY)
            continue
        if not r.ok:
            raise GitCodeError(r.status_code, _extract_error(r))
        return r.json()


def get_repo(owner, repo, token):
    """获取仓库基本信息（含默认分支）。"""
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}", token)


def list_branches(owner, repo, token):
    """列出仓库所有分支。"""
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}/branches", token)


def create_branch(owner, repo, branch_name, refs, token):
    """在指定 ref（分支名或 commit SHA）上创建新分支。"""
    return _do_request("POST", f"{BASE_URL}/repos/{owner}/{repo}/branches", token,
                       json={"refs": refs, "branch_name": branch_name})


def get_contents(owner, repo, path, ref, token):
    """查询指定路径上文件是否存在，存在则返回文件元信息（含 sha），
    供后续 update_file 使用；返回 None 表示文件不存在（404）。"""
    try:
        return _do_request("GET",
                           f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                           token, params={"ref": ref})
    except GitCodeError as e:
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
        raise GitCodeError(200, f"路径不是目录: {path or '/'}")
    return result


def get_file_text(owner, repo, path, ref, token):
    """读取指定文件的文本内容（base64 解码为 UTF-8 字符串）。

    文件不存在时抛出 GitCodeError(404)。
    """
    result = _do_request("GET",
                         f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                         token, params={"ref": ref})
    content_b64 = result.get("content", "")
    # API 返回的 base64 可能按行折叠，需先去除空白字符
    raw = base64.b64decode("".join(content_b64.split()))
    return raw.decode("utf-8")


def create_file(owner, repo, path, message, content_b64, branch, token):
    """在指定分支上创建新文件，content 需为 base64 编码。"""
    return _do_request("POST",
                       f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                       token, json={
                           "message": message,
                           "content": content_b64,
                           "branch": branch,
                       })


def update_file(owner, repo, path, message, content_b64, sha, branch, token):
    """更新已有文件，需提供当前文件的 sha 以防止并发覆盖。"""
    return _do_request("PUT",
                       f"{BASE_URL}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                       token, json={
                           "message": message,
                           "content": content_b64,
                           "sha": sha,
                           "branch": branch,
                       })


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


def list_pull_requests(owner, repo, token, state="open", head=None, base=None):
    """列出仓库的 Pull Request（默认仅 open），供提交前去重。

    head 格式为 "用户名:分支名"，base 为目标分支名；
    两者均作为 query 参数传给 API 做服务端过滤。
    """
    params = {"state": state, "per_page": 100}
    if head:
        params["head"] = head
    if base:
        params["base"] = base
    return _do_request("GET", f"{BASE_URL}/repos/{owner}/{repo}/pulls", token,
                       params=params)


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
                       json=payload)


def _extract_error(r):
    """从响应体中提取错误消息：优先取 message/error 字段，否则回退到原文。"""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or r.text
        return r.text
    except Exception:
        return r.text
