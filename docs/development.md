# 开发与部署

## 本地环境

使用 Python 3.11+。在仓库根目录创建 `.venv` 并安装 `requirements.txt`。静态页面直接由 Python 服务提供，无需 npm。复制 `.env.example` 为 `.env` 后填写自己的模型配置；进程环境变量优先于 `.env`，修改配置后需要重启服务。

| 配置 | 用途 |
| --- | --- |
| `PR_HARNESS_HOST` / `PR_HARNESS_PORT` | 默认 127.0.0.1 / 18080 |
| `PR_HARNESS_LLM_PROVIDER` | deepseek、custom 或其他受支持的服务商 |
| `PR_HARNESS_DEEPSEEK_API_KEY` | DeepSeek 凭据 |
| `PR_HARNESS_LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | custom 模型接入；完整变量均带 `PR_HARNESS_` 前缀 |
| `PR_HARNESS_DB_PATH` | 本地 SQLite，默认 `pr_harness.db` |
| `PR_HARNESS_DATABASE_URL` / `REDIS_URL` | 可选 PostgreSQL / Redis，完整变量带统一前缀 |
| `PR_HARNESS_REPAIR_TEST_COMMAND` | 目标仓库的真实测试命令 |
| `PR_HARNESS_AUTH_REQUIRED` | 本地模板 false，部署时开启并配置认证凭据 |

完整字段见根目录 `.env.example` 与 `pr_harness/core/config.py`。使用受管理的 GitHub 启动器时，模型、GitHub 凭据和 Cloudflared 均需可用；纯本地启动只需要 `-Local`。

## 回归测试

```sh
python -B -m unittest discover -s tests -v
```

使用项目虚拟环境中的 Python。2026-11-08 本地结果：73 项，68 通过，5 跳过。缺失的两份固定语料及自动恢复测试的方法见 [评测数据说明](../evaluation_data/README.md)。跳过项不计入已验证的评测效果，测试日志会显示跳过原因。

常规测试使用临时数据库、离线 diff 和模型夹具，不需要真实模型密钥，也不发布 GitHub 评论。后续配置持续集成时可使用 Python 3.11 和 3.13 矩阵运行相同命令。

## Compose

`docker compose up -d --build` 使用 Python 3.11 镜像、PostgreSQL 16 和 Redis 7。容器内部服务端口固定 8080，主机端口读取 `PR_HARNESS_PORT`（默认 18080）。Compose 强制开启登录，部署前通过 `.env` 设置独立的 `PR_HARNESS_AUTH_SECRET`、管理员用户名和密码。

本地 SQLite 与 Compose PostgreSQL 数据分别保存。更换存储配置不会自动迁移历史任务；`docker compose down` 保留数据卷。

## 修改约定

- HTTP 层放在 `api/`，业务编排放在 `core/` 与相应领域模块。
- 新工具放在 `tooling/` 并接入运行时权限与取证链路。
- 调整审查策略时检查报告门禁、反馈记忆和恢复行为。
- 修复功能应使用目标仓库自己的测试命令，不把其他示例项目的命令写为全局默认值。
- 提交前运行现有回归；新增外部依赖时同步 `requirements.txt` 和部署说明。

本机 `.env`、数据库、`validation/` 启动器状态和 `output/` 评测结果已忽略。历史整理快照、原始模型调用及实验结果从源码树移到了项目外归档。
