# 可选评测语料

当前发布包含评测代码、指标与离线单元测试，不包含以下两份原始语料：

| 文件 | 原有测试约定 |
| --- | --- |
| `pr_diff_100.jsonl` | 100 个 offline-fixture 案例、10 个仓库，40 个有问题样本与 60 个干净样本 |
| `prompt_evolution_130.jsonl` | 130 个提示词回放案例，按仓库隔离 validation 与 holdout |

两份语料在整理前已缺失，未用新生成的数据代替原始数据，也未据此重算效果指标。依赖它们的 5 项语料测试通过 `skipUnless` 明确跳过；其余测试继续执行。

如能取得原始数据，把经过来源和使用权限核对的文件放入本目录，再运行完整测试即可自动启用相应检查。数据文件默认由 `.gitignore` 排除，避免意外提交外部仓库内容或评测数据。

JSONL 每行一个对象，通常包含 `id`、`repository`、`pull_request`、`split`、`source`、`diff` 和 `expected_findings`。具体校验与匹配规则见 `pr_harness/evaluation/harness.py`、`experiments.py` 和 `evolution_proof.py`。生产数据集还需要满足 `benchmark.py` 中更严格的来源、标注和仓库隔离要求。

真实 PR 导入可使用 `scripts/import_github_pr_dataset.py --help`；公开 PR 内容本身不等于正确的问题标签，需要单独核对标注与修复验证证据。导入另一份数据不能替代固定语料的历史断言。
