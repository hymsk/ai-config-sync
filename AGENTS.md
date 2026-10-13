# AGENTS.md

本仓库维护 `ai-config-sync`：一个通过专用 Git 配置仓库单向归档/还原多主机配置的命令行工具。开始任务先阅读本文件和 [SPEC.md](./SPEC.md)。默认使用中文沟通；代码、命令、路径和 API 名称保留英文。

## 安全边界

- 以 [SPEC.md](./SPEC.md) 为当前行为契约；实现、测试和文档必须与其一致，改行为先改 SPEC。
- Git 操作永不 rebase、不 force push、不 reset、不 clean、**不做三方合并**：本地分支只允许 `--ff-only` 快进对齐远端；每条命令强制 `core.autocrlf=false`，哈希对比以字节保真为前提。
- 分叉永不自动解决：保留 Git 现场，报 `error-conflict` 交人工处理。同文件双侧修改在 `push` 下**不构成冲突**——push 是镜像语义，本机内容覆盖远端（本地优先）；删除沿 push 方向传播（本机已删的受管文件从仓库移除），`pull` 永不删除本机文件。
- 工具自身目录（`/etc/ai-config-sync`、`/var/lib/ai-config-sync`、工作树、安装目录）硬排除，任何清单规则不得把工具自己归档出去；凭据文件默认排除，仅在仓库所有者显式授权且明确放行后才可纳入 sync.list（见 SPEC 第 2 节）。
- URL 内嵌凭据一律拒绝；凭据文件默认不纳入清单，经所有者显式授权后放行则视为知情决定（明文凭据将进入 Git 历史并分发到所有同步主机）。
- 不为未发布的旧路径、旧配置和旧行为增加兼容代码。

## 开发要求

- 保持零外部 Python 依赖（Python >= 3.9）；配置解析只用自带 `tomlite`。
- 修改后运行 `python3 -m unittest discover -s tests -t .` 和 `git diff --check`；为新失败路径补隔离测试（本地 bare 仓库模拟远端、注入式 Layout 模拟多机，不访问真实远端）。
- SPEC、双语 README（`README.md` 英文 / `README.zh-cn.md` 中文）与实现同步更新，行为描述以 SPEC 为准。
- 两个操作 `push`/`pull` 均为手动一次性触发，不引入常驻进程或自动双向收敛。
- 未明确要求时不执行 `git add`、`git commit` 或 `git push`；不使用破坏性 Git 操作。
- 发布、打 tag 和创建 Release 是独立授权范围，见 [RELEASING.md](./RELEASING.md)。
