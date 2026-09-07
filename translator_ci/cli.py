"""命令行入口：扫描译稿变更并批量提交 PR（Jenkins 无人值守）。

用法：
    translator-ci [--keyword 关键字] [--rules-path token_rules.json]

凭据全部来自环境变量（Jenkins 凭据注入），不读任何本地配置文件：
    GITCODE_TOKEN            默认 Token（必需；读译稿权威仓库、未命中
                             规则的仓库也用它）
    规则文件中的 token_env   各规则 Token 对应的环境变量名

本地化模式（绕开 API 限流，由 Jenkinsfile 克隆权威仓库后注入）：
    GITCODE_LOCAL_SOURCE_DIR 权威仓库的本地克隆目录（设置后索引/列文件
                             走本地 git，不再调用 API）
    GITCODE_CLONE_DIR        目标仓库本地克隆目录（设置后目标对比也本地
                             化；未设置则目标对比回退 API）

退出码：
    0  成功（含「没有变更」）
    1  运行失败（Token 缺失、规则文件损坏、扫描失败等）
    2  扫描成功但部分仓库提交失败
"""

import argparse
import os
import sys

from translator_ci import service


def _log(msg):
    """日志直接写 stdout 并立即刷新，进 Jenkins 控制台。"""
    print(msg, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="translator-ci",
        description="扫描译稿权威仓库的变更，向目标社区仓库批量提交 PR")
    parser.add_argument("--keyword", default="",
                        help="按译稿路径关键字过滤扫描范围（调试用，默认全部）")
    parser.add_argument("--rules-path", default=service.DEFAULT_RULES_PATH,
                        help="权威仓库中 Token 规则文件的路径"
                             "（默认 %(default)s）")
    parser.add_argument("--blacklist-path",
                        default=service.DEFAULT_BLACKLIST_PATH,
                        help="权威仓库中拉黑名单文件的路径"
                             "（默认 %(default)s）")
    parser.add_argument("--sync-source", action="store_true",
                        help="来源为 Fork 时，扫描前先同步其与上游")
    parser.add_argument("--diff-before", default=os.environ.get("GIT_PUSH_BEFORE", ""),
                        help="增量模式：本次推送的 before sha（默认读环境"
                             "变量 GIT_PUSH_BEFORE，由 webhook 注入）")
    parser.add_argument("--diff-after", default=os.environ.get("GIT_PUSH_AFTER", ""),
                        help="增量模式：本次推送的 after sha（默认读环境"
                             "变量 GIT_PUSH_AFTER，由 webhook 注入）")
    parser.add_argument("--full-scan", action="store_true",
                        help="强制全量扫描（忽略增量参数，手动构建重试用）")
    args = parser.parse_args(argv)

    token = os.environ.get("GITCODE_TOKEN", "").strip()
    if not token:
        _log("错误：未配置 GITCODE_TOKEN 环境变量（默认 Token）")
        return 1

    # 本地化模式：权威仓库已在 agent 上克隆（Jenkinsfile 注入）
    local_source_dir = os.environ.get("GITCODE_LOCAL_SOURCE_DIR", "").strip() or None
    clone_root = os.environ.get("GITCODE_CLONE_DIR", "").strip() or None
    if local_source_dir:
        _log(f"本地化模式：索引来源 {local_source_dir}，目标克隆目录 "
             f"{clone_root or '未设置（目标对比回退 API）'}")

    if args.sync_source:
        service.sync_source(token, log=_log)

    # ── 加载 Token 规则（权威仓库内的规则文件）──
    try:
        token_rules = service.load_token_rules(
            token, rules_path=args.rules_path, log=_log,
            source_repo_dir=local_source_dir)
    except service.ConfigError as e:
        _log(f"错误：{e}")
        return 1
    except service.client.GitCodeError as e:
        _log(f"错误：读取规则文件失败: {e}")
        return 1

    # ── 加载拉黑名单（权威仓库内的名单文件，命中则跳过不处理）──
    try:
        blacklist = service.load_blacklist(
            token, blacklist_path=args.blacklist_path, log=_log,
            source_repo_dir=local_source_dir)
    except service.ConfigError as e:
        _log(f"错误：{e}")
        return 1
    except service.client.GitCodeError as e:
        _log(f"错误：读取拉黑名单失败: {e}")
        return 1

    # ── 扫描变更 ──
    _log("开始扫描全部译稿仓库..."
         if not args.keyword else f"开始扫描（关键字: {args.keyword}）...")
    diff_before = None if args.full_scan else (args.diff_before or "").strip() or None
    diff_after = None if args.full_scan else (args.diff_after or "").strip() or None
    result = service.scan_changed(
        args.keyword, token, token_rules=token_rules, blacklist=blacklist,
        log=_log, source_repo_dir=local_source_dir, clone_root=clone_root,
        diff_before=diff_before, diff_after=diff_after)
    if not result["ok"]:
        _log(f"扫描失败（阶段: {result.get('stage', 'unknown')}）"
             f": {result.get('error', '未知错误')}")
        return 1

    changed = result["changed"]
    mode = "增量" if result.get("incremental") else "全量"
    _log(f"扫描完成（{mode}）：{len(changed)} 个仓库有变更，"
         f"{len(result['unchanged'])} 个无变更。")
    for s in result["skipped"]:
        _log(f"  [跳过] {s['path']}: {s['reason']}")
    for e in result["errors"]:
        _log(f"  [失败] {e['path']}: {e['error']}")
    if not changed:
        _log("没有需要提交的变更。")
        return 0

    # ── 批量提交 ──
    _log(f"开始批量提交 {len(changed)} 个仓库的 PR...")
    batch = service.submit_all(changed, token, token_rules=token_rules, log=_log)
    if not batch["ok"]:
        _log(f"批量提交失败（阶段: {batch.get('stage', 'unknown')}）"
             f": {batch.get('error', '未知错误')}")
        return 1

    for r in batch["results"]:
        if r["ok"]:
            suffix = "（已存在未合并 PR，变更已并入）" if r.get("pr_existed") else ""
            _log(f"  [成功] {r['path']}{suffix}\n"
                 f"         PR: {r.get('pr_url', '-')}")
        else:
            _log(f"  [失败] {r['path']}（阶段: {r.get('stage', 'unknown')}）"
                 f": {r.get('error', '未知错误')}")
    _log(f"批量提交完成：成功 {batch['submitted']} 个，"
         f"失败 {batch['failed']} 个。")
    return 2 if batch["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
