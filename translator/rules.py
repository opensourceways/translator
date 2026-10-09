"""Token 规则与黑/白名单：读取、校验与按目录路径路由（权限/范围管控）。

规则与名单文件存于译稿权威仓库（默认根目录 token_rules.json、
blacklist.json、whitelist.json），每次运行时读取；Token 规则文件不存在
视为未配置规则（全部使用默认 Token），拉黑名单不存在视为未拉黑（全部
正常处理），开放白名单不存在视为不限制开放范围（全部正常处理）；配置了
有效规则或白名单时只处理命中的仓库。

规则文件只保存 keyword → 环境变量名（token_env）的映射，Token 本体
不落任何仓库，运行时从环境变量解析（Jenkins 凭据注入）。
"""

import json
import os

from translator import client, localgit
from translator.common import (
    _source_repo, DEFAULT_RULES_PATH, DEFAULT_BLACKLIST_PATH,
    DEFAULT_WHITELIST_PATH, ConfigError,
)


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


def load_token_rules(token, rules_path=DEFAULT_RULES_PATH, log=print,
                     source_repo_dir=None):
    """从译稿权威仓库读取 Token 规则文件并解析为 [{"keyword", "token"}]。

    规则文件只保存 keyword → 环境变量名的映射，Token 本体不落任何仓库：
    每条规则的 token_env 在运行时解析为环境变量（Jenkins 凭据注入）的值，
    环境变量缺失的规则打印警告并跳过。

    文件不存在（404）时返回空规则——全部仓库使用默认 Token；
    文件存在但 JSON 非法时抛出 ConfigError——权限边界配置损坏不能
    静默放行。source_repo_dir 非空时（本地化模式）从本地克隆读取，
    否则走 API。

    Returns:
        list: [{"keyword": ..., "token": ...}]，无有效规则时为空列表
    """
    owner, repo = _source_repo()
    log(f"正在从来源仓库 {owner}/{repo} 读取规则文件: {rules_path}")
    if source_repo_dir:
        try:
            text = localgit.read_file(source_repo_dir, rules_path).decode(
                "utf-8")
        except client.GitCodeError as e:
            if e.status_code == 404:
                log(f"来源仓库中不存在规则文件 {rules_path}，全部仓库使用默认 Token")
                return []
            raise
    else:
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


def _load_path_list(token, config_path, label, missing_log, log,
                    source_repo_dir):
    """从译稿权威仓库读取「仓库目录路径列表」配置文件（黑/白名单同构）。

    文件不存在（404）时返回空列表（视为未配置，不做任何限制）；
    文件存在但 JSON 非法或缺 repos 列表时抛出 ConfigError——范围边界
    配置损坏不能静默放行（否则可能意外放开全部仓库）。
    source_repo_dir 非空时（本地化模式）从本地克隆读取，否则走 API。

    Returns:
        list: 去空格、去首尾斜杠后的非空路径字符串，无有效条目时为空列表
    """
    owner, repo = _source_repo()
    log(f"正在从来源仓库 {owner}/{repo} 读取{label}: {config_path}")
    if source_repo_dir:
        try:
            text = localgit.read_file(source_repo_dir, config_path).decode(
                "utf-8")
        except client.GitCodeError as e:
            if e.status_code == 404:
                log(missing_log)
                return []
            raise
    else:
        repo_info = client.get_repo(owner, repo, token)
        branch = repo_info.get("default_branch") or "master"
        try:
            text = client.get_file_text(
                owner, repo, config_path, branch, token)
        except client.GitCodeError as e:
            if e.status_code == 404:
                log(missing_log)
                return []
            raise
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{label}文件 {config_path} JSON 解析失败: {e}")
    entries = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ConfigError(f"{label}文件 {config_path} 格式非法：缺少 repos 列表")

    paths = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, str):
            log(f"  [警告] 第 {i} 条{label}条目不是字符串，已跳过")
            continue
        value = entry.strip().strip("/")
        if not value:
            log(f"  [警告] 第 {i} 条{label}条目为空，已跳过")
            continue
        paths.append(value)
    return paths


def load_blacklist(token, blacklist_path=DEFAULT_BLACKLIST_PATH, log=print,
                   source_repo_dir=None):
    """从译稿权威仓库读取拉黑名单（不处理的仓库目录路径列表）。

    黑名单文件与 Token 规则一样存于权威仓库（默认根目录 blacklist.json），
    业务侧可直接增删条目，无需改动代码。被拉黑的仓库（目录路径等于名单
    条目或以其为前缀）在扫描阶段直接记入 skipped，不做对比也不建 PR。

    文件不存在（404）时返回空名单——全部仓库正常处理；
    文件存在但 JSON 非法时抛出 ConfigError——拉黑配置损坏不能静默放行。
    source_repo_dir 非空时（本地化模式）从本地克隆读取，否则走 API。

    Returns:
        list: 仓库目录路径字符串（去空格、非空），无名单时为空列表
    """
    blacklist = _load_path_list(
        token, blacklist_path, "拉黑名单",
        f"来源仓库中不存在拉黑名单 {blacklist_path}，全部仓库正常处理",
        log, source_repo_dir)
    if blacklist:
        log(f"已加载 {len(blacklist)} 条拉黑名单: {', '.join(blacklist)}")
    else:
        log("无有效拉黑名单，全部仓库正常处理")
    return blacklist


def load_whitelist(token, whitelist_path=DEFAULT_WHITELIST_PATH, log=print,
                   source_repo_dir=None):
    """从译稿权威仓库读取开放白名单（只处理的仓库目录路径列表）。

    白名单文件与 Token 规则、拉黑名单一样存于权威仓库（默认根目录
    whitelist.json），业务侧可直接增删条目，无需改动代码。白名单非空时
    只处理命中的仓库（目录路径等于条目或以其为前缀），其余记入 skipped；
    文件不存在或为空时不限制开放范围，全部仓库正常处理。

    文件不存在（404）时返回空名单——不限制开放范围；
    文件存在但 JSON 非法时抛出 ConfigError——范围边界配置损坏不能
    静默放行。source_repo_dir 非空时（本地化模式）从本地克隆读取，
    否则走 API。

    Returns:
        list: 仓库目录路径字符串（去空格、非空），无名单时为空列表
    """
    whitelist = _load_path_list(
        token, whitelist_path, "开放白名单",
        f"来源仓库中不存在开放白名单 {whitelist_path}，不限制开放范围",
        log, source_repo_dir)
    if whitelist:
        log(f"已加载 {len(whitelist)} 条开放白名单: {', '.join(whitelist)}")
    else:
        log("无有效开放白名单，不限制开放范围")
    return whitelist
