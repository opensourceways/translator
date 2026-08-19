// 译稿自动提 PR 流水线：GitCode Webhook（Push 事件）触发，
// 扫描译稿权威仓库变更并向目标社区仓库批量提交 PR。
// 要求：Jenkins 安装 Generic Webhook Trigger 插件；agent 有 Python 3.10+。
pipeline {
    agent any

    options {
        // 固定分支 + open PR 去重的前提是单并发，禁止并行构建写同一分支
        disableConcurrentBuilds()
    }

    triggers {
        GenericTrigger(
            genericVariables: [
                [key: 'ref', value: '$.ref']
            ],
            causeString: 'GitCode Push: $ref',
            token: 'translator-auto-pr'
        )
    }

    stages {
        stage('Setup') {
            steps {
                checkout scm
                sh 'python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt'
            }
        }
        stage('AutoPR') {
            steps {
                // Token 由任务的 Password Parameter 注入为环境变量
                // （参数名须与权威仓库 token_rules.json 中的 token_env 一致；
                // 新增一套规则 Token = 规则文件加一行 + 任务加一个密码参数）
                sh '.venv/bin/python -m translator_ci'
            }
        }
    }
}
