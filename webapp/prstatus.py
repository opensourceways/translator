"""PR 派生状态：把落库的原始信号映射为面板展示状态与下一步指引。

设计要点（存原始信号、展示层派生）：
- prs 表只存原始信号（state/mergeable/labels_json/draft/评审计数），
  派生状态由本模块纯函数 derive 在渲染/API 层计算——调整映射规则
  不需要重新轮询
- 整条优先级链是数据驱动的规则表（_RULES），按序取第一个命中；
  后续加规则只需在表中加一行
- 文案面向翻译平台同学（非工程师）：说人话、给出下一步该找谁
"""

import json


def _label_names(pr):
    """从 pr dict 提取小写标签名列表（兼容 labels 列表与 labels_json 串）。"""
    labels = pr.get("labels")
    if labels is None:
        raw = pr.get("labels_json")
        if raw:
            try:
                labels = json.loads(raw)
            except ValueError:
                labels = []
    names = []
    for label in labels or []:
        if isinstance(label, dict):
            label = label.get("name") or ""
        names.append(str(label).lower())
    return names


def _rule(key, text, color, guidance, pred):
    """一条派生规则：pred(pr, 小写标签名列表) 为真即命中。"""
    return {"key": key, "text": text, "color": color,
            "guidance": guidance, "pred": pred}


# 派生规则表：按优先级从高到低排列，取第一个命中；
# guidance 中 {accepted}/{total} 由 derive 用评审计数格式化
_RULES = [
    _rule("merged", "已合入", "green", "无需操作",
          lambda pr, names: pr.get("state") == "merged"),
    _rule("closed", "已关闭未合入", "gray",
          "确认是否被人工关闭；若仍有译稿变更，"
          "服务下次运行会重建分支并重新提交 PR",
          lambda pr, names: pr.get("state") == "closed"),
    _rule("draft", "草稿", "gray", "PR 为草稿状态，等待就绪",
          lambda pr, names: bool(pr.get("draft"))),
    _rule("ci_failed", "CI 未通过", "red",
          "目标仓库流水线未通过：点开 PR 查看 CI 日志；"
          "若为译稿内容问题（如格式校验）请修正后推送，服务会自动更新 PR；"
          "若为仓库自身 CI 问题请联系仓库维护者重跑",
          lambda pr, names: any(
              "ci" in n and ("fail" in n or "error" in n) for n in names)),
    _rule("conflict", "存在冲突", "orange",
          "无需人工处理：服务下次运行会以目标分支为基点重建分支，"
          "冲突通常自动消解；若长期冲突请检查目标仓库目录结构是否变更",
          lambda pr, names: pr.get("mergeable") is not None
          and not pr.get("mergeable")),
    _rule("cla_failed", "CLA 未通过", "orange",
          "commit 作者邮箱未签署 CLA，请联系服务运维检查提交账号的 CLA 签署",
          lambda pr, names: any(
              "cla" in n and n.endswith("/no") for n in names)),
    _rule("reviewing", "待评审", "blue",
          "等待仓库 maintainer 评审（{accepted}/{total} 已通过），无需操作",
          lambda pr, names: (pr.get("review_total") or 0) > 0
          and (pr.get("review_accepted") or 0) < (pr.get("review_total") or 0)),
    _rule("mergeable", "待合入", "green",
          "已满足合入条件，等待仓库 maintainer 合入",
          lambda pr, names: bool(pr.get("mergeable"))),
    _rule("submitted", "已提交", "blue", "PR 已创建，等待目标仓库处理",
          lambda pr, names: True),
]


def derive(pr):
    """把 prs 行（dict）映射为派生状态。

    Args:
        pr: prs 表行 dict（或带 labels/draft/mergeable 等键的同形 dict）。

    Returns:
        dict: {key, text, color, guidance}；color 为
        green/red/orange/blue/gray（模板徽章类名 d-<color>）。
    """
    names = _label_names(pr)
    for rule in _RULES:
        if rule["pred"](pr, names):
            guidance = rule["guidance"].format(
                accepted=pr.get("review_accepted") or 0,
                total=pr.get("review_total") or 0)
            return {"key": rule["key"], "text": rule["text"],
                    "color": rule["color"], "guidance": guidance}
    raise AssertionError("规则表兜底规则缺失")  # submitted 恒真，不可达
