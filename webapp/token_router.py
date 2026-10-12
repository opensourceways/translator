"""仓库 Token 路由：按仓库配置的命名 token 名称从 Vault 解析实际值。

命名 token 的明文只存在于 Vault 注入的 secrets 文件（键
``TR_TOKEN_<名称大写>``），服务侧 SQLite 只保存名称引用
（``repo_configs.token_name``），因此本模块不含任何缓存。

未配置名称的仓库使用默认 ``GITCODE_TOKEN``；配置了名称但 Vault 中取不到
值时判为配置错误，由调用方按失败处理（fail-closed，不静默回退默认 token，
避免用错误的身份向目标仓库提交 PR）。
"""

from translator import secrets


def resolve_repo_token(db, org, repo, default_token):
    """解析仓库提交/轮询应使用的 Token。

    Returns:
        tuple: (token, token_name, error)
        - 未配置命名 token：(default_token, "", None)
        - 名称非法或值缺失：("", name, "错误说明")
        - 正常：(value, name, None)
    """
    name = db.get_repo_token_name(org, repo)
    if not name:
        return default_token, "", None
    if not secrets.is_valid_token_name(name):
        return "", name, (
            f"命名 Token 名称「{name}」非法（应为小写字母/数字/下划线）")
    value = secrets.get_named_token(name)
    if not value:
        return "", name, (
            f"命名 Token「{name}」未在 Vault 配置"
            f"（缺少 {secrets.TOKEN_PREFIX}{name.upper()}）")
    return value, name, None
