# PR Harness

面向 GitHub Pull Request 的多 Agent 代码审查服务。Lead 分派任务，Security 与 Correctness/Reliability 取证审查，Critic 校验候选问题，最后汇总报告。包含任务恢复、反馈记忆、修复验证、评测以及提示词和 Skill 版本管理。

后端使用 Python，管理页面为原生 HTML / CSS / JavaScript，无需 Node.js。API 审查使用 agentic 模式，需要可用的模型配置；离线测试使用测试夹具。

## 快速启动

要求 Python 3.11+。PowerShell 在本地执行：

```powershell
git clone https://github.com/fkfk912912/PR-Harness.git
cd PR-Harness
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env，配置模型凭据
.\Start-PRHarness.ps1 -Local
```

例如使用 DeepSeek：

```dotenv
PR_HARNESS_LLM_PROVIDER=deepseek
PR_HARNESS_DEEPSEEK_API_KEY=填写自己的模型密钥
PR_HARNESS_LLM_MODEL=填写账号实际可用的模型ID
```

打开 http://127.0.0.1:18080/，粘贴 diff 发起审查；`/health` 可检查服务状态。按 Ctrl+C 停止。默认配置仅监听本机，未配置模型时健康检查仍可能通过。

用 VS Code 打开项目根目录，按 F5 启动后端、Cloudflare 隧道和 GitHub 自动审查，按 Shift+F5 时由清理任务停用 Webhook 并关闭后端和隧道。自动模式需要可用的模型、已登录的 GitHub CLI、仓库 Webhook 管理权限及 Cloudflared。直接运行 `.\Start-PRHarness.ps1` 同样启动自动审查；`-Local` 只启动本地服务，`-Check` 检查本地配置与资源。

Linux / macOS 使用 `python3 -m venv .venv`、`.venv/bin/python -m pip install -r requirements.txt`，复制配置后在项目根目录执行 `.venv/bin/python -m pr_harness`。

## GitHub 自动审查

需要 GitHub CLI、Cloudflared、目标仓库 Webhook 管理权限与 PR 评论权限。

```powershell
gh auth login
.\Start-PRHarness.ps1 -Repository owner/repository
# 检查配置：.\Start-PRHarness.ps1 -Check
# 停止受管理的服务：.\Start-PRHarness.ps1 -Stop
```

启动器建立签名校验入口与 Cloudflare 隧道，创建或更新目标仓库 Webhook，并启用报告回评。此命令会修改目标仓库的 Webhook 并允许服务发布审查评论。配置、权限和生命周期说明见 [GitHub 自动审查](docs/GitHub-PR自动审查.md)。

未指定仓库时，自动模式依次读取 `PR_HARNESS_GITHUB_REPOSITORY`、当前项目的 GitHub `origin`、历史会话记录。每次 F5 会先清理本项目的旧会话，再创建新隧道并更新原 Webhook 地址。

## 目录

```text
PR-Harness/
├── pr_harness/          后端业务与 HTTP 服务
├── web/                 管理页面
├── skills/              各审查领域的提示词包
├── scripts/             GitHub 启动器、数据导入与评测命令
├── tests/               离线回归与模型夹具
├── evaluation_data/     可选评测语料的格式和接入说明
├── docs/                使用、接口、目录与开发说明
├── .env.example         不含真实凭据的配置模板
├── docker-compose.yml   PostgreSQL + Redis 部署
└── Start-PRHarness.ps1   Windows 启动、检查与停止
```

模块路径和主流程见 [目录与流程](docs/目录与流程.md)，API 见 [运行与接口](docs/运行与接口.md)，日常使用见 [操作指南](docs/日常启动与审阅指南.md)。

## 测试与部署

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
docker compose config --quiet
docker compose up -d --build
```

本次本地回归共 73 项：68 项通过，5 项因缺少可选评测语料而明确跳过。恢复原始语料后这 5 项会自动执行，详见 [评测数据说明](evaluation_data/README.md)。当前结果不构成真实模型准确率或生产效果结论。

本地使用 SQLite 与内存队列；Compose 使用 PostgreSQL 和 Redis，属于独立的运行配置。部署前设置自己的认证密钥和管理员凭据。更多配置、测试与贡献说明见 [开发与部署](docs/development.md)。

`.env`、数据库、模型调用轨迹、运行日志、下载的可执行文件和历史快照均不进入 Git。已运行实例的 `validation/github-webhook/` 保留在本机，供启动器恢复和停止使用。
