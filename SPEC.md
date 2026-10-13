# ai-config-sync 规范

## 1. 目的

`ai-config-sync` 是一个纯规则驱动的多服务器配置同步 Agent：通过一个专用 Git 配置仓库，按配置的间隔自动收集本机受管文件的变化、推送远端，并把远端变化写回本机。发生合并冲突时**只暂停本机**（在运行用户 Home 下创建空文件 `ERROR_MERGE_FLAG`），保留 Git 冲突现场等待人工处理；删除标记且复核通过后自动恢复同步。

无 LLM、无外部 Python 依赖；Python >= 3.9，自带极简 TOML 解析器。

## 2. 范围

- 仅支持 Linux（依赖 `fcntl.flock`）；以 root 运行，目录布局为固定绝对路径。
- 只同步**普通文件**；软链接与非常规文件跳过并告警。
- 文件删除不自动传播：本机缺失、仓库有 → 下轮重建；远端删除 → 本机保留。
- 所有机器共用同一分支、同一清单与同一配置；无机器差异。
- 凭据文件（token、私钥、API key 等）默认排除，**不建议**纳入；经仓库所有者显式授权后可纳入清单（例如 `/root/.codex/auth.json`、`/root/.config/api-keys`）。放行意味着明文凭据进入 Git 历史并复制到**每一个**同步节点：私有仓库不等于安全，授权前应自行评估并准备轮换 token。

## 3. 两个仓库与目录布局

| 仓库 | 内容 |
|---|---|
| `ai-config-sync`（本仓库） | Agent 程序、种子模板、systemd 单元 |
| 配置仓库（如 `ai-shared-config`） | `.sync-config/`（运行配置与清单）+ `source/`（受管文件镜像） |

`source/` 下路径与主机绝对路径一一对应：`/root/.config/opencode/AGENTS.md` ↔ `source/root/.config/opencode/AGENTS.md`。部署后运行配置以配置仓库的 `.sync-config/` 为准；本仓库种子模板只在 `install new` 时使用，运行期绝不回退。

Agent 自身目录布局（`-p` 可改工作树位置）：

```text
/etc/ai-config-sync/bootstrap.toml   # 安装器生成：repo_url / branch / worktree / install_dir
/var/lib/ai-config-sync/repo         # 配置仓库本地 clone（工作树）
/var/lib/ai-config-sync/state/       # journal.jsonl / backups/ / sync.log / lock
$HOME/ERROR_MERGE_FLAG               # 冲突暂停标记（唯一 $HOME 依赖）
```

Agent 自身目录（`/etc/ai-config-sync`、`/var/lib/ai-config-sync`、工作树、安装目录、flag）为硬排除区，任何清单规则都不能把 Agent 自己同步出去。

## 4. 安装

- `install new <repo-url> [-b <branch>] [-p <path>]`：目标仓库必须为空；从种子模板初始化为配置仓库并推送。`--force` 允许在非空仓库中以孤儿分支播种（纯新增 ref，不碰已有分支），此时必须显式 `-b` 指定不存在的分支名。
- `install attach <repo-url> [-b <branch>] [-p <path>]`：远端分支必须存在且 `.sync-config/config.toml` 可解析。
- 仅接受 Git URL；URL 内嵌 `user:pass@` 一律拒绝，认证走本机 SSH key 或 credential helper。
- `-b` 省略时跟随 git 默认：attach 回读 `origin/HEAD`；new 跟随远端通告的 unborn HEAD 或本地 `init.defaultBranch`。解析结果固化进 bootstrap。
- bootstrap 或工作树已存在时一律拒绝安装；重新绑定 = 手动清理后重装。
- 安装器只写 bootstrap 和工作树，不注册 systemd（部署单元见 `deploy/`）。

## 5. sync.list 清单

语法类 .gitignore 但**默认不管理任何文件**：

- 仅支持绝对路径；普通行 = 纳入，`!` 开头 = 排除，后规则胜出。
- `*` 不跨 `/`；`**` 跨目录段；末段字面量按目录语义匹配整个子树。
- 规则字面前缀落在硬排除区或遍历禁区（`/`、`/proc`、`/sys`、`/dev`、`/run`）→ 配置错误，本轮暂停。
- 指向 `.git` 的规则拒绝；宽规则展开时命中的硬排除子树剔除并告警。
- 展开只收普通文件；软链接跳过并告警。
- 仓库侧已有、清单命中但本机缺失的路径也纳入受管集合（用于重建与预览），本机存在但被展开排除的路径只合并不写回。

## 6. 配置

`bootstrap.toml`（安装器生成，本机所有）：`repo_url`、`branch`、`worktree`、`install_dir`，均必填（`install_dir` 可为空串）。

`.sync-config/config.toml`（配置仓库，全局生效）：

| 字段 | 默认 | 语义 |
|---|---|---|
| `interval` | 5 | 本机检查间隔（秒），必须为正 |
| `remote_check_interval` | = `interval` | 无本机变化时的远端检查间隔（秒），必须为正 |
| `max_backups_per_file` | 20 | 每个文件保留的写回前备份份数；0 = 不清理，不得为负 |
| `[git] name` / `email` | `sync-bot` / `sync-bot@invalid` | 同步提交身份 |

任一字段非法即 `paused-invalid-config`；配置缺失同样暂停，不猜测。

## 7. 单轮事务（P0-P7）

```text
P0 预检：存在未完成合并 → 置标记并按冲突处理；工作树有未知改动 → paused-needs-doctor
P1 读取生效配置与清单展开（稳定读：两次哈希一致才采信）
P2 本机 vs 轮始 HEAD（A）归类变更：内容哈希不同的文件进入收集集
P3 变更写入工作树 source/... 并提交（仅显式路径，不联网）
P4 fetch --prune + merge origin/<branch>（同一文件双侧修改 → 强制冲突，置标记保留现场）
P5 push（non-FF → fetch+merge 重试 ≤3 次，仍失败 → error-transient）
P6 写回本机（见第 10 节）
P7 journal checkpointed；下轮 A = 当前 HEAD
```

- 采集判定全部是"主机文件 SHA-256 <-> Git blob 字节 SHA-256"即时对比；不维护 per-file 状态文件。
- 无本机变更、无缺失文件、无未稳定文件、跟踪 ref 未动且距上次 fetch 不足 `remote_check_interval` 时短路返回 `idle`。
- 远端认证/权限失败返回 `error-auth`；网络失败返回 `error-transient` 并指数退避（最多 2^6 倍 interval）。本地文件系统 `Permission denied` 不是认证错误。
- 每轮全程持有 `state/lock` 独占 flock；第二个实例立即失败，退出码 3。

## 8. 冲突处理

**冲突定义**：同一受管文件被双侧（本机与远端）相对基线同时修改，即视为冲突并强制交人工——即使行级差异不重叠、git 可以自动融合。文本可自动合并不等于应该自动合并：配置文件的变更意图以文件为单位，行级融合可能产出任何一侧都不想要、且语法仍合法的混合结果。单侧修改或双侧修改不同文件不受影响，仍由 git 自动收敛。

- 实现为 merge driver：`.gitattributes`/`.git/info/attributes` 对 `source/**` 与 `.sync-config/**` 声明 `merge=aics-conflict`，driver 定义为 `false`（永远失败）。agent 的 merge 命令始终通过 `-c` 注入 driver 定义；`run_once` 每轮幂等写入本地兜底（info/attributes + 本地 config），使存量配置仓库自动获得保护，手工 `git merge` 同样生效。
- 冲突时：保留 Git 冲突现场（不 reset、不 clean），置 `~/ERROR_MERGE_FLAG`，journal 记录冲突文件在冲突时刻的主机哈希，状态 `paused-conflict`。主机文件不受污染。
- 冲突现场形态：工作树中该文件为 ours 一侧的完整内容（无 `<<<<<<<` 标记），两侧完整内容始终可经 `git show :2:<path>`（ours）/ `:3:<path>`（theirs）获取。
- 首轮 `--prefer merge` 采用的 add/add 冲突遵循同一现场形态（ours=仓库版 theirs=本机版）；运行期冲突 ours=本机采集版 theirs=远端版。
- 冲突只暂停本机，其他服务器继续同步。
- 恢复流程：人工在工作树解决（整文件选边 `git checkout --ours/--theirs -- <path>`，或编辑后 `add`），然后 `commit --no-edit`，再删除标记。下一轮复核：合并未完成 → 重新置标记；工作树未提交内容 → `paused-needs-doctor`；复核通过 → 按记录的主机哈希写回解决结果（主机仍是冲突时刻内容才写；暂停期间又被编辑则留给下轮收集），随后下一轮推送解决提交。
- 只删标记不解决冲突会被重新置标记。

## 9. 首轮采用

```bash
ai-config-sync once                 # 仅预览差异（differs/host-only/repo-only/unstable/same）
ai-config-sync once --prefer local  # 本机为准：收集并推送（覆盖远端版本）
ai-config-sync once --prefer repo   # 仓库为准：备份后写回本机
ai-config-sync once --prefer merge  # 三方裁决：双侧不同的文件产生 add/add 冲突
                                     # 标记（ours=仓库版 theirs=本机版），逐文件人工解决
```

- `--prefer` 只作用于"双侧都有且内容不同"的文件；单侧文件（仅本机/仅仓库）在任何取值下都向双侧收敛。
- 三种方向都先 fetch 并整合远端（仅快进/干净合并；真实分叉冲突按普通冲突流程暂停）。
- `prefer merge` 的合并提交推送失败不回滚：本地 HEAD 已含合并结果，返回 `error-transient`，由下一轮正常事务重试推送。

## 10. 写回契约

对每个本轮相对 A 有变化（或本机缺失）的受管文件：

1. **写前复核**：主机仍等于 A 基线 → 安全写回；等于本机采集提交 L → 跳过（内容已被合并保留）；否则视为轮中被修改，不覆盖，计入 `waiting-local-stable`。
2. **格式校验**：`.json` / `.toml` 按可解析性校验，失败跳过并计入 skipped；其他格式放行。
3. **备份**：写回前把主机原文件复制到 `state/backups/<run_id>/<repo_path>`；每次写回后按 `max_backups_per_file` 修剪同一文件的历史备份（按 run_id 排序，0 = 不清理）。
4. **原子替换**：同目录临时文件 + fsync + rename + 目录 fsync；保留既有 mode/uid/gid，新建文件 mode 0644。
5. 远端删除不传播：合并结果中不存在该 blob 时本机保留不动。

## 11. 崩溃恢复

- journal 为追加式 JSONL，唯一本机状态；每阶段先 fsync 再执行动作。
- 重启后按最后一条事务记录恢复：`captured` → 从 P4 继续；`merged` → 从 P5 继续；`pushed` → 从 P6 继续；`applied` → 补 checkpoint。恢复路径与正常路径共用同一幂等逻辑（重复 merge 收敛于 "Already up to date"）。
- journal 任一行损坏 → 不猜测，`paused-needs-doctor`，由 `doctor` 辅助人工定基线。
- journal 超过 200 条时裁剪保留最近 100 条。

## 12. Git 操作不变量

- 不 rebase、不 force push、不 reset、不 clean；冲突永远保留现场。
- 同一文件双侧修改一律强制冲突（merge driver `aics-conflict` 定义为失败），不做行级自动融合；单侧修改与不同文件的自动收敛不受影响（见第 8 节）。
- 每条命令强制 `core.autocrlf=false`，种子仓库另配 `.gitattributes`（`* -text`），保证字节级保真（哈希对比的前提）。
- 所有 git 子进程带 120 秒超时（ls-remote 60 秒）。
- 提交只包含显式收集的路径；工作树出现任何未知改动即暂停等待人工。

## 13. 日志与诊断

- `state/sync.log` 轮转保留（1 MB × 5 份），同时镜像到 stderr。
- `doctor` 逐项体检 bootstrap / 远端连通 / 分支一致 / 工作树干净 / 无残留合并 / 标记状态 / 配置可解析 / journal 完整，全过退出 0。
- 退出码：0 成功或 idle；1 同步失败或安装失败；2 本机配置错误；3 锁被占用。

## 14. 已知边界与非目标

- 应用自身会重写的配置文件会造成同步回流，不要纳入清单。
- 不处理文件权限/属主的差异同步（仅写回时保留原值）。
- 不做多机调度、不感知其他主机的在线状态；收敛依赖各机独立轮次。
- 不提供单向镜像模式；同步始终是双向收敛。
- 默认不在配置仓库中保存凭据；经仓库所有者显式授权后，可将凭据文件纳入清单同步（风险与轮换责任由所有者承担，见第 2 节）。

## 15. 验证

- 本地门禁：`python3 -m unittest discover -s tests -t .` 与 `git diff --check`。
- 测试使用本地 bare 仓库模拟远端、注入式 Layout 模拟多机，覆盖：清单匹配与硬排除、双机同步、冲突暂停与人工恢复（含只删标记重置）、双侧修改同一文件不同区域仍强制冲突（含 add/add 现场两侧 stage 完整性）、非快进竞态、journal 断点恢复（captured/pushed）、轮中修改保护、删除重建、CRLF 字节保真、安装分支规则（含 --force 孤儿分支与 attach 默认分支）、首轮采用三种方向、认证判定边界、备份修剪、首轮采用推送失败恢复、强制冲突 driver 的幂等写入与本地兜底。
