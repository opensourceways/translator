"""译稿自动提 PR 的核心业务编排层（facade）。

对外暴露统一入口，内部按职责拆分到子模块，便于独立维护与测试：
  common:  共享常量、来源仓库解析与通用工具（ConfigError、_git_blob_sha）
  rules:   Token 规则读取与按路径路由（权限管控）
  index:   来源仓库同步与目录索引
  scan:    扫描对比，筛出有变更的仓库
  submit:  幂等提交 PR（Fork/固定分支/去重）

译稿权威来源为 GitCode 私有仓库
Computing_Ecosystem_Docs/Computing_Ecosystem_Docs-EN。
仓库内目录层级固定为「组织/版本/产品/仓库/目标路径」，例如
Ascend/26.1.0/MindStudio/msprof/docs/en/guide.md：
  第 1 段    目标社区组织名（org）
  第 2 段    目标分支名（版本号）
  第 3 段    产品名（仅作分组展示）
  第 4 段    目标仓库名（repo）
  第 5 段起  译稿文件在目标仓库中的真实存放路径

为 Jenkins 无人值守场景设计：
- 无状态：每次运行全量扫描对比（git blob sha 逐文件比对），不依赖任何
  本地缓存；两次触发之间的任意多个 commit 自动汇总为一个 PR
- 幂等提交：固定分支 translator/auto-<目标分支>、与分支现有内容相同的
  文件跳过写入、目标仓库已存在同一 head/base 的 open PR 时不重复创建
  （本次新写入的 commit 自动进入已开 PR）
- Token 规则（权限管控）：规则文件存于译稿权威仓库（默认根目录
  token_rules.json），每次运行时读取；文件不存在视为未配置规则（全部
  使用默认 Token）；配置了有效规则时只处理命中规则的仓库
- 拉黑名单（范围管控）：名单存于译稿权威仓库（默认根目录
  blacklist.json），目录路径命中名单的仓库直接跳过，不做对比也不建 PR
- 开放白名单（范围管控）：名单存于译稿权威仓库（默认根目录
  whitelist.json），名单非空时只处理命中的仓库、名单外一律跳过
  （如「功能先只对某仓库开放」），名单为空时不限制开放范围
- 来源可替换：默认读取权威仓库，可用 GITCODE_SOURCE_REPO 环境变量
  指向自己的 Fork（无权威仓库管理权限时测试用，Webhook 配在 Fork 上）；
  该模式下可用 sync_source 在扫描前先同步 Fork 与上游
- 本地化模式（绕开 API 限流）：由 Jenkinsfile 把权威仓库克隆到 agent
  （GITCODE_LOCAL_SOURCE_DIR）并指定目标克隆目录（GITCODE_CLONE_DIR），
  索引/列文件/对比走本地 git，API 只保留建 PR 等少量写操作；不设置
  则退回 API 模式（进程级限流兜底）

流程函数（入口）：
  load_token_rules: 读取并解析规则文件（JSON 非法抛 ConfigError）
  load_blacklist:   读取拉黑名单（目录路径列表，JSON 非法抛 ConfigError）
  load_whitelist:   读取开放白名单（目录路径列表，JSON 非法抛 ConfigError）
  sync_source:      来源为 Fork 时，扫描前同步其与上游
  scan_changed:     扫描全部仓库级目录 → 与目标仓库对比 → 有变更清单
  submit_repo:      单仓库幂等提交 PR
  submit_all:       对有变更仓库逐个幂等提交 PR，单仓失败不中断

任意阶段失败立即返回错误信息，已写入的文件不会回滚。
"""

from docsync import client
from docsync.common import (
    DEFAULT_RULES_PATH,
    DEFAULT_BLACKLIST_PATH,
    DEFAULT_WHITELIST_PATH,
    ConfigError,
)
from docsync.rules import (
    load_token_rules, load_blacklist, load_whitelist)
from docsync.index import sync_source
from docsync.scan import scan_changed
from docsync.submit import submit_repo, submit_all
