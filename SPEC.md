# ai-config-sync 规范

## 1. 目的

`ai-config-sync` 是一个纯规则驱动的手动单向归档工具：通过一个专用 Git 配置仓库，
提供两个显式操作，均由用户手动触发、无常驻进程：

- `push`（本机 → 仓库）：把本机受管文件归档为配置仓库 `source/` 的精确镜像，
  提交并推送远端。
- `pull`（仓库 → 本机）：把配置仓库中的受管文件写回本机，写前备份、格式校验、原子替换。
- `update`：快进安装仓库到远端最高 `vX.Y.Z` tag；`push`/`pull` 执行前自动做
  同样的检查（5s 超时，失败只警告不阻塞；有更新则更新后重新执行命令）。

无 LLM、无外部 Python 依赖；Python >= 3.9，自带极简 TOML 解析器。

## 2. 范围

- 仅支持 Linux；以 root 运行，目录布局为固定绝对路径。
- 只同步**普通文件**；软链接与非常规文件跳过并告警。
- 删除**单向传播**：`push` 把"本机已删的受管文件"从仓库移除（真镜像）；
  `pull` **永不删除**本机文件（仓库没有的路径本机保留）。
- 所有机器共用同一分支、同一清单与同一配置；无机器差异。
- 每次操作都是单向的：`push` 只写仓库、`pull` 只写本机，不做双向收敛、
  不做任何三方合并。
- 凭据文件（token、私钥、API key 等）默认排除，**不建议**纳入；经仓库所有者显式授权后可纳入清单（例如 `/root/.codex/auth.json`、`/root/.config/api-keys`）。放行意味着明文凭据进入 Git 历史并复制到**每一个**同步节点：私有仓库不等于安全，授权前应自行评估并准备轮换 token。

## 3. 两个仓库与目录布局

| 仓库 | 内容 |
|---|---|
| `ai-config-sync`（本仓库） | 工具程序、种子模板 |
| 配置仓库（如 `ai-config-stor`） | `.sync-config/`（运行配置与清单）+ `source/`（受管文件镜像） |

`source/` 下路径与主机绝对路径一一对应：`/root/.config/opencode/AGENTS.md`
↔ `source/root/.config/opencode/AGENTS.md`。部署后运行配置以配置仓库的
`.sync-config/` 为准；本仓库种子模板只在 `install new` 时使用，运行期绝不回退。

工具自身目录布局（`-p` 可改工作树位置）：

```text
/etc/ai-config-sync/bootstrap.toml   # 安装器生成：repo_url / branch / worktree / install_dir
/var/lib/ai-config-sync/repo         # 配置仓库本地 clone（工作树）
/var/lib/ai-config-sync/state/       # backups/ / sync.log / lock
```

工具自身目录（`/etc/ai-config-sync`、`/var/lib/ai-config-sync`、工作树、安装目录）
为硬排除区，任何清单规则都不能把工具自己同步出去。

## 4. 安装

- `install new <repo-url> [-b <branch>] [-p <path>]`：目标仓库必须为空；从种子模板初始化为配置仓库并推送。`--force` 允许在非空仓库中以孤儿分支播种（纯新增 ref，不碰已有分支），此时必须显式 `-b` 指定不存在的分支名。
- `install attach <repo-url> [-b <branch>] [-p <path>]`：远端分支必须存在且 `.sync-config/config.toml` 可解析。
- 仅接受 Git URL；URL 内嵌 `user:pass@` 一律拒绝，认证走本机 SSH key 或 credential helper。
- `-b` 省略时跟随 git 默认：attach 回读 `origin/HEAD`；new 跟随远端通告的 unborn HEAD 或本地 `init.defaultBranch`。解析结果固化进 bootstrap。
- bootstrap 或工作树已存在时一律拒绝安装；重新绑定 = 手动清理后重装。
- 安装器只写 bootstrap 和工作树，不注册任何服务（无常驻进程）。

## 5. sync.list 清单

语法类 .gitignore 但**默认不管理任何文件**：

- 仅支持绝对路径；普通行 = 纳入，`!` 开头 = 排除，后规则胜出。
- `*` 不跨 `/`；`**` 跨目录段；末段字面量按目录语义匹配整个子树。
- 规则字面前缀落在硬排除区或遍历禁区（`/`、`/proc`、`/sys`、`/dev`、`/run`）→ 配置错误，操作失败。
- 指向 `.git` 的规则拒绝；宽规则展开时命中的硬排除子树剔除并告警。
- 展开只收普通文件；软链接跳过并告警。
- `pull` 时仓库侧已有、清单命中但本机缺失的路径也纳入受管集合（用于重建）；本机存在但被展开排除的路径不参与写回。

## 6. 配置

`bootstrap.toml`（安装器生成，本机所有）：`repo_url`、`branch`、`worktree`、`install_dir`，均必填（`install_dir` 可为空串）。

`.sync-config/config.toml`（配置仓库，全局生效）：

| 字段 | 默认 | 语义 |
|---|---|---|
| `max_backups_per_file` | 20 | `pull` 写回前每个文件保留的历史备份份数；0 = 不清理，不得为负 |
| `[git] name` / `email` | `sync-bot` / `sync-bot@invalid` | 归档提交身份 |

任一字段非法即操作失败（`error-config`）；配置缺失同样失败，不猜测。

> 兼容字段 `interval` / `remote_check_interval` 已随常驻模式一并移除，不再读取。

## 7. push（本机 → 仓库）

```text
1. 取独占锁
2. fetch + 把本地分支**只快进**对齐远端（--ff-only，无三方合并）：
   - 本地落后 → 快进；一致 → 无操作
   - 本地领先（上轮推送失败残留的归档提交）→ 容忍，继续在其上追加
   - 真分叉（本地与远端各有对方没有的提交，通常为人工改动）→ error-conflict，
     保留现场交人工
3. 确保工作树干净（有未知改动 → error-dirty，人工处理）
4. 读取生效配置与清单展开（稳定读：两次哈希一致才采信）
5. 前缀存在性检查（见下）
6. 本机 vs HEAD：内容哈希不同的受管文件写入工作树 source/...
7. 镜像移除：命中清单规则、但本机已不存在的受管路径从仓库移除（git rm）
8. git add/rm 显式路径 + commit（不联网）
9. push；被拒（non-FF，推送竞态）→ error-conflict 提示重跑，不自动重试
10. 报告归档/移除文件数
```

- 采用判定是"主机文件 SHA-256 ↔ Git blob 字节 SHA-256"即时对比；不维护 per-file 状态文件。
- 同文件双侧修改（本机与远端）**不产生冲突**：push 是镜像语义，本机内容
  直接覆盖远端（本地优先）。想接受远端版本就先 `pull` 再 `push`。
- 未稳定（两次哈希不一致）的文件本轮跳过，下次再试。

### 前缀存在性检查（push 独有）

纳入规则（非 `!` 排除行）的**字面前缀**（第一个通配符之前的路径段）必须存在于本机：

- 显式文件规则（如 `/root/foo.conf`）指向的文件缺失 → `error-missing`；
- 目录规则（如 `/etc/xxx/**`）的目录 `/etc/xxx` 整体不存在 → `error-missing`；
  报错信息列出全部缺失规则，提示同步修改 sync.list 或恢复路径后重试。
- 前缀存在、目录内**具体文件**被删 → 不报错，走镜像移除静默处理。
- `pull` 不做此检查：本机缺失正是 pull 要重建的场景。

## 8. pull（仓库 → 本机）

```text
1. 取独占锁
2. fetch + 把本地分支**只快进**对齐远端（规则同 push 第 2 步）
3. 读取生效配置与清单展开（含"仓库有、清单命中、本机缺失"的路径，用于重建）
4. 对 HEAD 中每个命中清单的 source/... 文件：
   - 本机内容已一致 → 跳过
   - `.json` 格式校验失败 → 跳过并计入 skipped；`.toml` 见第 9 节校验规则
   - 写回前备份到 state/backups/<run_id>/<repo_path>，按 max_backups_per_file 修剪
   - 原子替换（保留既有 mode/uid/gid；新建 0644）
5. 报告写回/跳过文件数
```

- 仓库中没有的路径本机保留不动（pull 永不删除本机文件）。
- 软链接目标不覆盖。

## 9. 写回契约（pull）

1. **格式校验**：`.json` 按严格可解析性校验；`.toml` 优先用标准库 `tomllib`（Python 3.11+）或可选的 `tomli` 校验，两者都不可用时用自带 `tomlite` **宽松**校验——`tomlite` 只是子集解析器，其不支持的合法 TOML（如数组 `args = [...]`）**不得**当作校验失败。确证无法解析（如严格解析器报错）才跳过并计入 skipped；其他格式放行。
2. **备份**：写回前把主机原文件复制到 `state/backups/<run_id>/<repo_path>`；每次写回后按 `max_backups_per_file` 修剪同一文件的历史备份（按 run_id 排序，0 = 不清理）。
3. **原子替换**：同目录临时文件 + fsync + rename + 目录 fsync；保留既有 mode/uid/gid，新建文件 mode 0644。

## 10. Git 操作不变量

- 不 rebase、不 force push、不 reset、不 clean；**不做任何三方合并**：
  本地分支只允许 `--ff-only` 快进对齐远端；分叉一律报错保留现场交人工。
- 每条命令强制 `core.autocrlf=false`，种子仓库另配 `.gitattributes`（`* -text`），保证字节级保真（哈希对比的前提）。
- 所有 git 子进程带 120 秒超时（ls-remote 60 秒）。
- 提交只包含显式收集/移除的路径；工作树出现任何未知改动即拒绝操作等待人工。
- `push`/`pull` 每轮全程持有 `state/lock` 独占 flock；第二个实例立即失败，退出码 3。

## 11. update（自更新）

- 版本只看 tag：`git ls-remote --tags`（`--refs`，仅 `vX.Y.Z` 格式）取最高版本，
  与本地 `__version__` 比较；不比较分支，发布以 tag 为准。
- 更新 = 安装目录（`bootstrap.toml` 的 `install_dir`，须为 git 检出）执行
  `fetch --tags` + `merge --ff-only <tag>`；无三方合并、无 force、无 reset。
- 本地无法快进（历史分叉/被改写）→ 报错保留现场交人工。
- 非 git 检出安装（wheel 等）无法自更新：手动 `update` 明确报错，
  自动检查静默跳过。
- 超时：手动 `update` 60s；push/pull 前的自动检查 5s，失败只警告不阻塞。
- 自动检查有更新时更新安装仓库后以替换进程重新执行命令
  （环境变量 `AI_CONFIG_SYNC_NO_UPDATE=1` 防循环，re-exec 后跳过检查）。

## 12. 日志与诊断

- `state/sync.log` 轮转保留（1 MB × 5 份），同时镜像到 stderr。
- 退出码：0 成功；1 操作失败（网络/冲突/脏工作树/清单前缀缺失等）；2 本机配置错误；3 锁被占用。
- 状态码：`ok` / `error-dirty`（工作树脏）/ `error-conflict`（分叉或推送被拒）/
  `error-missing`（清单规则指向的本机路径不存在）/ `error-auth`（凭据/权限）/
  `error-transient`（网络）/ `error-remote`（远端分支消失）/ `error-config`（配置非法）/
  `error-state`（工作树无提交）。

## 13. 已知边界与非目标

- 应用自身会重写的配置文件会造成归档回流，不要纳入清单。
- 不处理文件权限/属主的差异同步（仅写回时保留原值）。
- 不做多机调度、不感知其他主机的在线状态；两个方向都由人工显式触发。
- 多机镜像语义：`push` 是"本机受管文件的全量镜像"，最后推送者的内容胜出。
  他机归档、而本机没有的文件，push 时会被移除；想保留请先 `pull` 再 `push`。
- 不提供自动双向收敛、常驻守护、崩溃恢复 journal 与冲突暂停标记。
- 默认不在配置仓库中保存凭据；经仓库所有者显式授权后，可将凭据文件纳入清单同步（风险与轮换责任由所有者承担，见第 2 节）。

## 14. 验证

- 本地门禁：`python3 -m unittest discover -s tests -t .` 与 `git diff --check`。
- 测试使用本地 bare 仓库模拟远端、注入式 Layout 模拟多机，覆盖：清单匹配与硬排除、
  `push` 归档与自动推送、镜像移除、清单前缀缺失报错、快进对齐与分叉报错、
  `pull` 写回与备份、格式校验跳过、软链接跳过、CRLF 字节保真、
  安装分支规则（含 `--force` 孤儿分支与 attach 默认分支）、
  认证判定边界、备份修剪、update 的 tag 解析/快进/分叉保护/非 git 安装。
