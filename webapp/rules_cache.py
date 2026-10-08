"""进程内共享最近一次 run 加载的 Token 规则（runner 写、prsync 读）。

规则本体（含解析后的 token 值）只驻留内存，不落 SQLite、不进日志。
服务重启后缓存为空，prsync 负责回退到 API 模式重新加载一次
（见 prsync._ensure_rules）。
"""

import threading

_lock = threading.Lock()
_rules = []


def set_rules(rules):
    """替换缓存的规则列表（runner 每次 run 加载规则后调用）。"""
    global _rules
    with _lock:
        _rules = list(rules or [])


def get_rules():
    """返回缓存规则的副本；从未加载过时为空列表。"""
    with _lock:
        return list(_rules)


def resolve_repo_token(db, org, repo, path, token_rules, default_token):
    """统一的仓库 token 路由：仓库配置的命名 token > token_rules.json
    关键字 > 默认。

    仓库配置（设置页）通过 token_name 引用 named_tokens 池中的 token，
    命中时优先使用（大小写不敏感）；否则回退引擎 rules.resolve_token
    的关键字路由。返回值结构与 rules.resolve_token 一致：
    (token, 命中说明)。
    """
    from translator_ci.rules import resolve_token
    token = db.get_repo_token(org, repo)
    if token:
        return token, "仓库配置的命名 Token"
    return resolve_token(path, token_rules, default_token)
