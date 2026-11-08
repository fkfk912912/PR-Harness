# GitHub PR 自动审查

## 准备

先完成 [本地安装](../README.md)，填写 `.env` 中的模型配置，再安装 GitHub CLI 与 Cloudflared。Windows 可使用：

```powershell
winget install --id GitHub.cli -e
winget install --id Cloudflare.cloudflared -e
gh auth login
```

安装后打开新终端，确保 `gh` 和 `cloudflared` 可在 PATH 中找到。启动器优先使用 PATH 中的 Cloudflared，同时兼容本机历史路径 `validation/github-webhook/cloudflared.exe`。自定义位置可直接运行 Python 启动器并传入 `--cloudflared`。

GitHub 身份需要目标仓库的 Webhook 管理权限、读取 PR 的权限，以及写审查评论的权限。若申请自动修复与 Draft PR，还需要相应的代码分支与 PR 写权限。

## 启动

```powershell
.\Start-PRHarness.ps1 -Repository owner/repository
```

启动器会启动 Python 服务、生成或读取 Webhook 签名密钥、建立专用签名入口与 Cloudflare Quick Tunnel，然后为指定仓库创建或更新 `pull_request` Webhook，并启用报告回评。管理台仅监听本机，隧道入口只转发目标仓库经过签名校验的 Webhook 请求。

保持终端运行。后续可直接 `.\Start-PRHarness.ps1` 使用保存的仓库记录。Python 启动器也可直接运行：

```sh
python scripts/run_github_webhook.py --repository owner/repository --cloudflared /path/to/cloudflared
```

凭据优先读取 `PR_HARNESS_GITHUB_TOKEN`，未配置时从已登录的 GitHub CLI 获取。调用过程不会把凭据写入源码。

## 停止与状态

按 Ctrl+C，或执行 `.\Start-PRHarness.ps1 -Stop`。正常退出时启动器尝试停用本次管理的 Webhook 并停止子进程；异常断电等情况下需在 GitHub 仓库设置中核对 Webhook 状态。

本机 `validation/github-webhook/status.json` 保存上次连接的仓库和 Webhook 编号，不应删除正在运行的会话状态。整个 `validation/` 被 Git 忽略，新克隆会由启动器自动创建所需目录。

| 现象 | 检查位置 |
| --- | --- |
| PR 事件未送达 | GitHub Webhook Recent Deliveries 与 `cloudflared.log` |
| 收到事件但任务失败 | 任务错误、模型配置与 `pr_harness.log` |
| 报告生成但无评论 | GitHub 凭据写权限与 API 返回错误 |
| 隧道未完成启动 | Cloudflared 安装、网络与当前终端输出 |
| 端口已占用 | 已有服务是否属于当前受管理会话 |

日志与状态均位于 `validation/github-webhook/`。Quick Tunnel 地址随重启改变，旧地址只在对应服务和隧道在线时有效。
