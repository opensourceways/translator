// 译稿自动提 PR 流水线：GitCode Webhook（Push 事件）触发，
// 扫描译稿权威仓库变更并向目标社区仓库批量提交 PR。
// 要求：Jenkins 安装 Generic Webhook Trigger 插件；agent 有 Python 3.10+。
pipeline {
    agent any

    options {
        // 固定分支 + open PR 去重的前提是单并发，禁止并行构建写同一分支
        disableConcurrentBuilds()
    }

    environment {
        // 本地化模式：权威仓库克隆到 agent 后，索引/列文件/对比走本地 git，
        // 彻底绕开 GitCode API 单账号 50 次/分钟限流（旧的逐目录 API 索引
        // 会发起上百次请求必然触发 429）。不设置则退回 API 模式（进程级限流兜底）。
        AUTHORITATIVE_DIR = "${WORKSPACE}/authoritative"
        CLONE_DIR = "${WORKSPACE}/clones"
        GITCODE_LOCAL_SOURCE_DIR = "${WORKSPACE}/authoritative"
        GITCODE_CLONE_DIR = "${WORKSPACE}/clones"
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
        stage('Clone') {
            steps {
                // 浅克隆译稿权威仓库（全量对象，含 blob——内容本地读取即时，
                // 不依赖 API 下载）；--no-checkout 不落工作区，只用对象库。
                // 鉴权用默认 Token（任务的密码参数，控制台自动脱敏）。
                sh '''
                    rm -rf "$AUTHORITATIVE_DIR" "$CLONE_DIR"
                    mkdir -p "$CLONE_DIR"
                    git clone --depth=1 --no-checkout \
                        "https://oauth2:${GITCODE_TOKEN}@gitcode.com/Computing_Ecosystem_Docs/Computing_Ecosystem_Docs-EN.git" \
                        "$AUTHORITATIVE_DIR"
                '''
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
