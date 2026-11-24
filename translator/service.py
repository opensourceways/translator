"""引擎门面导出。

HTTP 服务只用到「单仓幂等提交」这一项能力；历史上供 Jenkins 调用的
扫描/批量提交入口（scan_changed / submit_all / sync_source）已随
CLI 一并移除。
"""

from translator.submit import submit_repo

__all__ = ["submit_repo"]
