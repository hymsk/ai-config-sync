# ai-config-sync 规范

## 1. 目的

`ai-config-sync` 是一个纯规则驱动的手动单向归档工具：通过一个专用 Git 配置仓库，
提供四个原子操作与三个固定组合，均由用户手动触发、无常驻进程。

四个原子操作，每个只做一件事：

- `commit`（本机文件 → 本地 config）：把本机受管文件镜像写入工作树并**本地提交**，不联网。
- `push`（本地 config → 远端）：先把 `source/` 残留镜像按当前内容补提交，再推送本地提交，**不重算主机文件**。
- `sync`（本地 config → 本机）：把本地 HEAD 的受管文件写回本机（写前备份、格式校验、
  原子替换），**不联网、永不删除主机文件**。
- `pull`（远端 → 本地 config）：`fetch` 远端并**仅快进**对齐本地分支，**不碰主机文件**。

三个固定组合，按执行顺序书写（只接受这三种，`push commit` 等一律拒绝）：

| 组合 | 数据流 | 等价 |
|---|---|---|
| `commit push` | 本机文件 → 远端 | 归档并推送（以文件为准） |
| `push sync` | 本地 config → 远端 + 本机 | 以本地 git 为准 |
| `pull sync` | 远端 → 本机 | 拉取并写回（以远端为准） |

- `update`：快进安装仓库到远端最高 `vX.Y.Z` tag（纯手动命令，
  不在其他命令执行前自动检查）。
- `clear`：丢弃本地 config 工作树的全部未提交改动（显式破坏性命令，
  不参与组合，见第 13 节）。

无 LLM、无外部 Python 依赖；Python >= 3.9，自带极简 TOML 解析器。

## 2. 范围

- 仅支持 Linux；以 root 运行，目录布局为固定绝对路径。
- 只同步**普通文件**；软链接与非常规文件跳过并告警。
- 删除**单向传播**：`push` 把"本机已删的受管文件"从仓库移除（真镜像）；
  `pull` **永不删除**本机文件（仓库没有的路径本机保留）。
- 所有机器共用同一分支、同一清单与同一配置；无机器差异。
- 每次操作都是单向的：`commit`/`push` 只写仓库、`sync` 只写本机、`pull` 只写本地 config，
  不做双向收敛、不做任何三方合并。
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

## 7. commit（本机文件 → 本地 config）

```text
1. 确认当前分支即 bootstrap 分支（detached / 其他分支 → error-conflict）
2. 了结工作树残留：`source/` 下上次 commit 中断留下的镜像改动按**工作树当前内容**直接补提交（不重读主机文件）；其余未知改动 → error-dirty，人工处理
3. 读取生效配置与清单展开（稳定读：两次哈希一致才采信）
4. 前缀存在性检查（见下）
5. 本机 vs HEAD：内容哈希不同的受管文件写入工作树 source/...
6. 镜像移除：命中清单规则、但本机已不存在的受管路径从仓库移除（git rm）
7. git add/rm 显式路径 + commit（不联网）；无变动则不造空提交
8. 报告归档/移除文件数
```

- **不联网、不改远端**：`commit` 只把本机文件镜像进本地工作树并落地一个本地提交，
  推送由 `push` 负责。
- 采用判定是"主机文件 SHA-256 ↔ Git blob 字节 SHA-256"即时对比；不维护 per-file 状态文件。
- 未稳定（两次哈希不一致）的文件本轮跳过，下次再试。
- **中断恢复（commit / push 共用）**：上次 `commit` 若在"写入镜像之后、提交之前"
  中断，会在 `source/` 留下未提交改动（含未跟踪新镜像与删除）。`commit` 与
  `push` 开始时都会把这些残留按工作树当前内容补一个本地提交（文件存在 →
  `git add`；已删除 → `git rm`），本地无需人工善后即可继续提交/推送；
  `pull` 遇残留则拒绝并提示先执行 `push`/`commit`。`.sync-config` 与
  `source/` 之外的改动仍属未知改动。

### 前缀存在性检查（commit 独有）

纳入规则（非 `!` 排除行）的**字面前缀**（第一个通配符之前的路径段）必须存在于本机：

- 显式文件规则（如 `/root/foo.conf`）指向的文件缺失 → `error-missing`；
- 目录规则（如 `/etc/xxx/**`）的目录 `/etc/xxx` 整体不存在 → `error-missing`；
  报错信息列出全部缺失规则，提示同步修改 sync.list 或恢复路径后重试。
- 前缀存在、目录内**具体文件**被删 → 不报错，走镜像移除静默处理。
- `sync` 不做此检查：本机缺失正是 sync 要重建的场景。

## 8. push（本地 config → 远端）

```text
1. 确认当前分支即 bootstrap 分支（detached / 其他分支 → error-conflict）
2. 了结 source/ 镜像残留（同 commit 的中断恢复：按当前内容补提交；
   其余未知改动 → error-dirty）
3. fetch；比较本地 HEAD 与 origin/<branch>：
   - 远端分支不存在 → error-remote；本机无提交 → error-state
   - 一致 → 无操作（不造提交）
   - 本地落后（远端有本地没有的提交）→ error-conflict，**不自动快进**，
     提示改用 pull / pull sync
   - 真分叉（双方各有对方没有的提交）→ error-conflict，保留现场交人工
   - 本地领先 → 继续
4. 推送本地提交（含了结产生的提交；不重算主机文件）
5. push；被拒（non-FF，推送竞态）→ error-conflict，不自动重试
6. 报告推送提交数
```

- `push` **不读清单、不重算主机文件**：除把 `source/` 残留镜像按工作树当前
  内容补提交外不产生任何新归档；主机文件的最新改动仍由 `commit` 收集。
- 与旧模型不同，"同文件双侧修改"在拆分后**不再被本地覆盖**：若远端在你 commit
  期间前进，push 会以 `error-conflict` 拒绝（分叉）而不是覆盖。想让本机内容胜出，
  先 `pull`，在本地取舍后再 `commit push`；本机内容本已是本地提交时，直接
  `push sync` 即可。

## 9. sync（本地 config → 本机）

```text
1. 读取生效配置与清单展开（含"仓库有、清单命中、本机缺失"的路径，用于重建）
2. 对 HEAD 中每个命中清单的 source/... 文件：
   - 本机内容已一致 → 跳过
   - `.json` 格式校验失败 → 跳过并计入 skipped；`.toml` 见第 11 节校验规则
   - 写回前备份到 state/backups/<run_id>/<repo_path>，按 max_backups_per_file 修剪
   - 原子替换（保留既有 mode/uid/gid；新建 0644）
3. 报告写回/跳过文件数
```

- **不联网**：`sync` 只把本地 HEAD 的内容落到本机；从远端取内容由 `pull` 负责。
- 仓库中没有的路径本机保留不动（sync 永不删除本机文件）。
- 软链接目标不覆盖。

## 10. pull（远端 → 本地 config）

```text
1. 确认当前分支即 bootstrap 分支（detached / 其他分支 → error-conflict）
2. 确保工作树干净（未知改动或未了结的镜像残留 → error-dirty；
   后者可先执行 push/commit 自动了结）
3. fetch；比较本地 HEAD 与 origin/<branch>：
   - 远端分支不存在 → error-remote；本机无提交 → error-state
   - 一致 → 无操作
   - 本地领先（有未推送提交）→ 无操作（远端无新内容可拉）
   - 本地落后 → **只快进**（--ff-only，无三方合并）
   - 真分叉 → error-conflict，保留现场交人工
4. 报告是否快进
```

- `pull` **不碰主机文件**：只更新本地 config 分支；落到本机由 `sync` 负责。
- 组合 `pull sync`（= 旧的 `pull`）即"拉取并写回"。

## 11. 写回契约（sync / pull sync）

1. **格式校验**：`.json` 按严格可解析性校验；`.toml` 优先用标准库 `tomllib`（Python 3.11+）或可选的 `tomli` 校验，两者都不可用时用自带 `tomlite` **宽松**校验——`tomlite` 只是子集解析器，其不支持的合法 TOML（如数组 `args = [...]`）**不得**当作校验失败。确证无法解析（如严格解析器报错）才跳过并计入 skipped；其他格式放行。
2. **备份**：写回前把主机原文件复制到 `state/backups/<run_id>/<repo_path>`；每次写回后按 `max_backups_per_file` 修剪同一文件的历史备份（按 run_id 排序，0 = 不清理）。
3. **原子替换**：同目录临时文件 + fsync + rename + 目录 fsync；保留既有 mode/uid/gid，新建文件 mode 0644。

## 11. Git 操作不变量

- 不 rebase、不 force push、不 reset、不 clean（`clear` 命令是唯一显式例外：
  见第 13 节）；**不做任何三方合并**：
  只有 `pull` 会快进（`--ff-only`）本地分支对齐远端；`push` 遇远端领先/分叉一律报错；
  真分叉一律报错保留现场交人工。
- 每条命令强制 `core.autocrlf=false`，种子仓库另配 `.gitattributes`（`* -text`），保证字节级保真（哈希对比的前提）。
- 所有 git 子进程带 120 秒超时（ls-remote 60 秒）。
- `commit` 的提交只包含显式收集/移除的路径（含中断恢复的显式镜像路径），
  不做任何隐式暂存；`commit`/`push` 开始时把 `source/` 镜像残留按工作树
  当前内容补提交；`pull` 要求工作树干净（未知改动或未了结的镜像残留
  → error-dirty）。
- `commit`/`push`/`sync`/`pull` 的一次调用（含组合）全程持有 `state/lock` 独占 flock；
  第二个实例立即失败，退出码 3。

## 12. update（自更新）

- 纯手动命令：不在 commit/push/sync/pull 等命令执行前自动做任何检查。
- 版本只看 tag：`git ls-remote --tags`（`--refs`，仅 `vX.Y.Z` 格式）取最高版本，
  与本地 `__version__` 比较；不比较分支，发布以 tag 为准。
- 更新 = 安装目录（`bootstrap.toml` 的 `install_dir`，须为 git 检出）执行
  `fetch --tags` + `merge --ff-only <tag>`；无三方合并、无 force、无 reset。
- 本地无法快进（历史分叉/被改写）→ 报错保留现场交人工。
- 非 git 检出安装（wheel 等）无法自更新，明确报错。
- 超时：60s。

## 13. clear（丢弃本地未提交改动）

- 显式手动命令，不属于原子步骤，不参与组合；不联网、不碰主机文件。
- 语义 = `git reset --hard HEAD` + `git clean -fd`：已跟踪文件（含暂存区）
  恢复到 HEAD，未跟踪文件与目录移除；`.gitignore` 忽略的文件与嵌套
  git 仓库不动，若有残留报 error-dirty 交人工。
- 前置：当前分支即 bootstrap 分支且 HEAD 存在；全程持有独占锁；
  工作树已干净时为无操作（ok，不造提交）。
- 用途：一键清理 error-dirty 现场。对中断残留的 `source/` 镜像改动，
  push/commit 选择**补提交了结**（以工作树当前内容为准），clear 选择
  **直接丢弃**（以 HEAD 为准）。

## 14. 日志与诊断

- `state/sync.log` 轮转保留（1 MB × 5 份），同时镜像到 stderr。
- 退出码：0 成功；1 操作失败（网络/冲突/脏工作树/清单前缀缺失等）；2 本机配置错误；3 锁被占用。
- 状态码：`ok` / `error-dirty`（工作树脏）/ `error-conflict`（分叉或推送被拒）/
  `error-missing`（清单规则指向的本机路径不存在）/ `error-auth`（凭据/权限）/
  `error-transient`（网络）/ `error-remote`（远端分支消失）/ `error-config`（配置非法）/
  `error-state`（工作树无提交）。

## 15. 已知边界与非目标

- 应用自身会重写的配置文件会造成归档回流，不要纳入清单。
- 不处理文件权限/属主的差异同步（仅写回时保留原值）。
- 不做多机调度、不感知其他主机的在线状态；每一步都由人工显式触发。
- 多机镜像语义：`commit push` 是"本机受管文件的全量镜像"，最后推送者的内容胜出。
  他机归档、而本机没有的文件，会被镜像移除；想保留请先 `pull sync` 再 `commit push`。
- `sync` 不感知远端、`pull` 不感知主机文件：`pull sync` 才是"以远端为准"的完整写回；
  `push` 不重算主机文件、`sync` 不联网：`push sync` 才是"以本地 git 为准"。
- 不提供自动双向收敛、常驻守护、崩溃恢复 journal 与冲突暂停标记。
- 默认不在配置仓库中保存凭据；经仓库所有者显式授权后，可将凭据文件纳入清单同步（风险与轮换责任由所有者承担，见第 2 节）。

## 16. 验证

- 本地门禁：`python3 -m unittest discover -s tests -t .` 与 `git diff --check`。
- 测试使用本地 bare 仓库模拟远端、注入式 Layout 模拟多机，覆盖：清单匹配与硬排除、
  `commit` 本地归档与镜像移除、`push` 推送（含中断残留自动补提交）与远端领先/分叉拒绝、
  `sync` 写回与备份、`pull` 仅快进与分叉拒绝、
  组合 `commit push` / `push sync` / `pull sync`、异常组合拒绝、
  清单前缀缺失报错、格式校验跳过、软链接跳过、CRLF 字节保真、
  clear 丢弃全部未提交改动与干净幂等、
  安装分支规则（含 `--force` 孤儿分支与 attach 默认分支）、
  认证判定边界、备份修剪、update 的 tag 解析/快进/分叉保护/非 git 安装。
