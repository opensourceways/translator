"""webapp.prstatus（PR 派生状态映射）的单元测试。

纯函数测试：输入为 prs 行同形 dict，覆盖规则表全部优先级分支
（含真实标签组合 ci-pipeline-failed + cann-cla/yes）。
"""

from webapp.prstatus import derive

CI_FAILED = [{"name": "ci-pipeline-failed", "color": "#DB2828"}]
CLA_YES = [{"name": "cann-cla/yes", "color": "#21BA45"}]
CLA_NO = [{"name": "cann-cla/no", "color": "#DB2828"}]


def _pr(**kw):
    """构造一条最小 PR 行（默认 open、无任何信号 → 兜底已提交）。"""
    row = {"state": "open", "mergeable": None, "draft": 0,
           "review_total": 0, "review_accepted": 0, "labels_json": None}
    row.update(kw)
    return row


# ── 终态与草稿（最高优先级） ──────────────────────────────────


class TestTerminalAndDraft:
    def test_merged_beats_all_signals(self):
        """merged 优先于一切（即便带 CI 失败标签与冲突）。"""
        d = derive(_pr(state="merged", mergeable=0, labels=CI_FAILED))
        assert d["key"] == "merged"
        assert d["color"] == "green"
        assert d["guidance"] == "无需操作"

    def test_closed_beats_ci_label(self):
        """closed 优先于 CI 失败标签。"""
        d = derive(_pr(state="closed", labels=CI_FAILED))
        assert d["key"] == "closed"
        assert d["color"] == "gray"
        assert "重新提交 PR" in d["guidance"]

    def test_draft_beats_ci_label(self):
        d = derive(_pr(draft=1, labels=CI_FAILED))
        assert d["key"] == "draft"
        assert d["color"] == "gray"


# ── 标签规则 ──────────────────────────────────────────────────


class TestLabelRules:
    def test_real_label_combo_ci_failed(self):
        """真实组合 ci-pipeline-failed + cann-cla/yes → CI 未通过。"""
        d = derive(_pr(labels=CI_FAILED + CLA_YES))
        assert d["key"] == "ci_failed"
        assert d["color"] == "red"
        assert "CI 日志" in d["guidance"]

    def test_labels_json_string_input(self):
        """labels_json 字符串（库中原始形态）同样可判定。"""
        d = derive(_pr(labels_json='[{"name": "ci-pipeline-failed",'
                                   ' "color": "#DB2828"}]'))
        assert d["key"] == "ci_failed"

    def test_label_match_case_insensitive(self):
        d = derive(_pr(labels=[{"name": "CI-Pipeline-Failed"}]))
        assert d["key"] == "ci_failed"

    def test_cla_no(self):
        """cla 且以 /no 结尾 → CLA 未通过。"""
        d = derive(_pr(labels=CLA_NO))
        assert d["key"] == "cla_failed"
        assert d["color"] == "orange"
        assert "CLA" in d["guidance"]

    def test_cla_yes_not_failure(self):
        d = derive(_pr(labels=CLA_YES))
        assert d["key"] == "submitted"

    def test_ci_beats_cla(self):
        """CI 失败优先级高于 CLA 未通过。"""
        d = derive(_pr(labels=CI_FAILED + CLA_NO))
        assert d["key"] == "ci_failed"


# ── 冲突 / 评审 / 可合入 / 兜底 ────────────────────────────────


class TestSignals:
    def test_conflict(self):
        """mergeable 为假（0 或 False）→ 存在冲突，优先于 CLA。"""
        d = derive(_pr(mergeable=0, labels=CLA_NO))
        assert d["key"] == "conflict"
        assert d["color"] == "orange"
        assert "无需人工处理" in d["guidance"]
        assert derive(_pr(mergeable=False))["key"] == "conflict"

    def test_reviewing_partial(self):
        """部分评审通过 → 待评审，指引带进度计数。"""
        d = derive(_pr(review_total=2, review_accepted=1))
        assert d["key"] == "reviewing"
        assert d["color"] == "blue"
        assert "1/2" in d["guidance"]
        # 无人通过也算待评审
        assert derive(_pr(review_total=1))["key"] == "reviewing"

    def test_reviewing_beats_mergeable(self):
        """待评审优先级高于可合入。"""
        d = derive(_pr(mergeable=1, review_total=2, review_accepted=1))
        assert d["key"] == "reviewing"

    def test_all_reviewed_falls_through(self):
        """评审全部通过且 mergeable 未计算 → 兜底已提交。"""
        d = derive(_pr(review_total=2, review_accepted=2))
        assert d["key"] == "submitted"

    def test_mergeable(self):
        d = derive(_pr(mergeable=1))
        assert d["key"] == "mergeable"
        assert d["color"] == "green"
        assert "待合入" == d["text"]

    def test_fallback_submitted(self):
        d = derive(_pr())
        assert d["key"] == "submitted"
        assert d["color"] == "blue"
        assert d["guidance"]
