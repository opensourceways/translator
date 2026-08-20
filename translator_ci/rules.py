"""Token 规则：读取、校验与按目录路径路由 Token（权限管控）。

规则文件存于译稿权威仓库（默认根目录 token_rules.json），每次运行时
读取；文件不存在视为未配置规则（全部使用默认 Token）；配置了有效
规则时只处理命中规则的仓库。

规则文件只保存 keyword → 环境变量名（token_env）的映射，Token 本体
不落任何仓库，运行时从环境变量解析（Jenkins 凭据注入）。
"""

import json
import os

from translator_ci import client
from translator_ci.common import _source_repo, DEFAULT_RULES_PATH, ConfigError


def resolve_token(path, token_rules, default_token):
    """按 Token 规则为译稿目录选择访问目标仓库的 Token。

    规则关键字对目录完整路径（如 Ascend/26.1.0/MindStudio/msprof）
    做大小写不敏感的包含匹配，命中第一条规则即返回其 Token；
    关键字或 Token 留空的规则视为无效并跳过；全部未命中时返回
    默认 Token。

    Returns:
        tuple: (token, matched_keyword)，未命中规则时 keyword 为 None
    """
    for rule in token_rules or []:
        if not isinstance(rule, dict):
            continue
        keyword = (rule.get("keyword") or "").strip()
        token = (rule.get("token") or "").strip()
        if keyword and token and keyword.lower() in path.lower():
            return token, keyword
    return default_token, None


def _effective_rules(token_rules):
    """筛出有效 Token 规则（关键字与 Token 均非空的字典条目）。"""
    return [
        r for r in token_rules or []
        if isinstance(r, dict)
        and (r.get("keyword") or "").strip()
        and (r.get("token") or "").strip()
    ]


def load_token_rules(token, rules_path=DEFAULT_RULES_PATH, log=print):
    """从译稿权威仓库读取 Token 规则文件并解析为 [{"keyword", "token"}]。

    规则文件只保存 keyword → 环境变量名的映射，Token 本体不落任何仓库：
    每条规则的 token_env 在运行时解析为环境变量（Jenkins 凭据注入）的值，
    环境变量缺失的规则打印警告并跳过。

    文件不存在（404）时返回空规则——全部仓库使用默认 Token；
    文件存在但 JSON 非法时抛出 ConfigError——权限边界配置损坏不能
    静默放行。

    Returns:
        list: [{"keyword": ..., "token": ...}]，无有效规则时为空列表
    """
    owner, repo = _source_repo()
    log(f"正在从来源仓库 {owner}/{repo} 读取规则文件: {rules_path}")
    repo_info = client.get_repo(owner, repo, token)
    branch = repo_info.get("default_branch") or "master"
    try:
        text = client.get_file_text(
            owner, repo, rules_path, branch, token)
    except client.GitCodeError as e:
        if e.status_code == 404:
            log(f"来源仓库中不存在规则文件 {rules_path}，全部仓库使用默认 Token")
            return []
        raise
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"规则文件 {rules_path} JSON 解析失败: {e}")
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        raise ConfigError(f"规则文件 {rules_path} 格式非法：缺少 rules 列表")

    resolved = []
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            log(f"  [警告] 第 {i} 条规则不是对象，已跳过")
            continue
        keyword = (rule.get("keyword") or "").strip()
        env_name = (rule.get("token_env") or "").strip()
        if not keyword or not env_name:
            log(f"  [警告] 第 {i} 条规则缺少 keyword 或 token_env，已跳过")
            continue
        value = os.environ.get(env_name, "").strip()
        if not value:
            log(f"  [警告] 规则「{keyword}」对应的环境变量 {env_name} 未配置，已跳过")
            continue
        # 打印脱敏指纹（前 3 位 + 长度），便于排查参数值配错的问题
        log(f"  规则「{keyword}」Token 指纹: {value[:3]}***（长度 {len(value)}）")
        resolved.append({"keyword": keyword, "token": value})

    if resolved:
        log(f"已加载 {len(resolved)} 条 Token 规则（仅处理命中规则的仓库）")
    else:
        log("无有效 Token 规则，全部仓库使用默认 Token")
    return resolved
