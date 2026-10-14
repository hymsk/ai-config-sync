# ai-config-sync

> 通过专用 Git 仓库，在 Linux 主机之间单向归档与还原配置文件。

一个规则驱动的命令行工具，提供四个原子操作与三个固定组合，无常驻进程、
无自动双向收敛、**不做任何合并**。每个原子步骤只做一件事：

- `commit`（本机文件 → 本地 config）：把本机受管文件镜像写入工作树并**本地提交**，不联网。
- `push`（本地 config → 远端）：只把本地已有提交推送到远端，不重算主机文件。
- `sync`（本地 config → 本机）：把本地 HEAD 的受管文件写回本机（写前备份、格式校验、
  原子替换），不联网、**永不删除主机文件**。
- `pull`（远端 → 本地 config）：`fetch` 远端并**仅快进**对齐本地分支，**不碰主机文件**。

三个固定组合，按执行顺序书写（只接受这三种，`push commit` 等一律拒绝）：

| 组合 | 数据流 | 等价 |
|---|---|---|
| `commit push` | 本机文件 → 远端 | 归档并推送（以文件为准） |
| `push sync` | 本地 config → 远端 + 本机 | 以本地 git 为准 |
| `pull sync` | 远端 → 本机 | 拉取并写回（以远端为准） |

[English](README.md) · [规范](SPEC.md) · [版本记录](CHANGELOG.md)

- 纯规则驱动，无 LLM、无外部 Python 依赖（Python >= 3.9，自带极简 TOML 解析）。
- 全程不 rebase、不 force push、不 reset、不 clean（独立命令 `clear` 是
  唯一显式例外）、**不做三方合并**：
  只有 `pull` 会 `--ff-only` 快进本地分支；`push` 遇远端领先/分叉一律报错；
  真分叉一律报错保留现场交人工。
- 以文件为准：所有变更判定都是"主机文件 Hash <-> Git blob Hash"即时对比，
  不维护 per-file 状态文件。

## 两个仓库

| 仓库 | 内容 |
|---|---|
| `ai-config-sync`（本仓库） | 工具程序、种子模板 |
| 配置仓库（如 `ai-config-stor`） | `.sync-config/`（运行配置与纳入清单）+ `source/`（受管文件镜像） |

配置仓库中 `source/` 下的路径与主机绝对路径一一对应：

```text
主机 /root/.config/opencode/AGENTS.md
  <-> source/root/.config/opencode/AGENTS.md
```

部署后运行配置以配置仓库的 `.sync-config/config.toml` 与 `sync.list` 为准；
本仓库的种子模板只在 `install new` 时使用，运行期绝不回退。

## 目录布局（固定绝对路径；root 运行）

```text
/etc/ai-config-sync/bootstrap.toml   # 安装器生成：repo_url / branch / worktree
/var/lib/ai-config-sync/repo         # 配置仓库本地 clone（工作树，-p 可改）
/var/lib/ai-config-sync/state/       # backups/ / sync.log / lock
```

工具自身目录被硬排除，任何清单规则都不能把工具自己归档出去。

## 安装

```bash
# 新建：目标仓库必须为空；从种子模板初始化为配置仓库
ai-config-sync install new <repo-url> [-b <branch>] [-p <path>]
#   --force：在非空仓库中以孤儿分支播种（纯新增 ref，不碰已有分支；
#            需显式 -b 指定不存在的分支名）

# 接入：目标仓库须已有分支且 .sync-config/config.toml 可解析
ai-config-sync install attach <repo-url> [-b <branch>] [-p <path>]
```

- 仅接受 Git URL；URL 内嵌 `user:pass@` 会被拒绝，认证走本机 SSH key 或
  credential helper。
- `-b` 省略时跟随 git 默认：attach 回读 `origin/HEAD`；new 跟随远端通告的
  unborn HEAD 或本地 `init.defaultBranch`。解析结果固化进 bootstrap。
- bootstrap 或工作树已存在时一律拒绝；重新绑定 = 手动清理后重装。

## 使用

```bash
ai-config-sync commit       # 本机文件 → 本地 config（镜像写入 + 本地提交）
ai-config-sync push         # 本地 config → 远端（只推送本地提交）
ai-config-sync sync         # 本地 config → 本机（写回受管文件，写前备份）
ai-config-sync pull         # 远端 → 本地 config（仅快进，不碰主机文件）
ai-config-sync commit push  # 本机文件 → 远端（归档并推送）
ai-config-sync push sync    # 本地 config → 远端 + 本机（以本地 git 为准）
ai-config-sync pull sync    # 远端 → 本机（拉取并写回）
ai-config-sync update       # 自更新：快进安装仓库到远端最新 vX.Y.Z tag
ai-config-sync clear        # 丢弃本地 config 工作树的全部未提交改动
```

每个操作都是一次性、手动触发。一次调用（含组合）全程持有独占锁；
第二个实例立即失败，退出码 3。

自更新严格手动触发：`ai-config-sync update` 查询工具自身远端的最高
`vX.Y.Z` tag（60s 超时），若比当前版本新，则把安装仓库快进到该 tag
（`--ff-only`，无合并、无 force）并报告结果；非 git 检出安装（如 wheel）
无法自更新，会明确提示。其他命令执行前不做任何自动检查。

### commit（本机文件 → 本地 config）

```text
1. 确认当前分支即 bootstrap 分支
2. 了结上次 commit 中断残留的 source/ 镜像改动（按工作树当前内容
   补一个本地提交）；其余未知改动 → error-dirty
3. 读取生效配置与清单展开（稳定读：两次 Hash 一致）
4. 前缀检查：每条纳入规则的字面前缀必须存在于本机
   （显式列出的文件没了，或 /etc/xxx/** 的 /etc/xxx 整个不存在
    → error-missing：先改 sync.list 或恢复路径再重跑；
    目录里具体文件被删不算错——走镜像移除）
5. 本机变更写入工作树 source/...
6. 镜像移除：命中清单但本机已不存在的受管路径从仓库 git rm
7. 仅显式路径本地提交（不联网）；无变动则不造空提交
8. 报告归档/移除文件数（逐行列出路径）
```

`commit` 不联网、不改远端：只把本机文件镜像进本地工作树并落地本地提交；
推送由 `push` 负责。

### push（本地 config → 远端）

```text
1. 要求 bootstrap 分支
2. 了结上次 commit 中断残留的 source/ 镜像改动（按当前内容补提交；
   其余未知改动 → error-dirty）
3. fetch；比较本地 HEAD 与 origin/<branch>：
   - 一致 → 无操作
   - 本地落后（远端有本地没有的提交）→ error-conflict，不自动快进
     （提示改用 pull / pull sync）
   - 真分叉 → error-conflict，保留现场交人工
   - 本地领先 → 继续
4. 推送本地提交（含了结产生的提交；不重算主机文件）
5. 被拒（non-FF 推送竞态）→ error-conflict，不自动重试
6. 报告推送提交数（逐条列出提交：短 sha + 标题）
```

`push` 不读清单、不重算主机文件：上次 commit 中断残留的 source/ 镜像改动
会先按工作树当前内容补提交并随之推送，全程无需人工进工作树善后。
与旧模型不同，拆分后"同文件双侧修改"
**不再被本地覆盖**：若远端在你 commit 期间前进，push 会以 `error-conflict`
（分叉）拒绝而非覆盖。想让本机内容胜出，先 `pull`，本地取舍后再 `commit push`；
本机内容已是本地提交时，直接 `push sync` 即可。

### sync（本地 config → 本机）

```text
1. 读取生效配置与清单展开（含"仓库有、清单命中、本机缺失"的路径——
   这正是 sync 要重建的内容）
2. 对 HEAD 中每个命中清单的 source/... 文件：
   - 本机内容已一致 → 跳过
   - .json/.toml 格式校验失败 → 跳过并计入 skipped
   - 写回前备份到 state/backups/<run_id>/<repo_path>，按配置修剪
   - 原子替换（保留既有 mode/uid/gid；新建 0644）
3. 报告写回/跳过文件数（逐行列出路径与跳过原因）
```

`sync` 不联网、永不删除本机文件：仓库没有的路径本机保留不动。

### pull（远端 → 本地 config）

```text
1. 要求 bootstrap 分支；要求工作树干净（中断残留的镜像改动
   可先经 push/commit 自动了结）
2. fetch；比较本地 HEAD 与 origin/<branch>：
   - 一致 → 无操作
   - 本地领先（有未推送提交）→ 无操作
   - 本地落后 → 仅快进（--ff-only，无三方合并）
   - 真分叉 → error-conflict，保留现场交人工
3. 报告是否快进（快进时逐条列出提交）
```

`pull` 不碰主机文件；落地由 `sync` 负责，正是组合 `pull sync` 所做的事。

### clear（丢弃本地未提交改动）

```text
1. 要求 bootstrap 分支且 HEAD 存在；全程持有独占锁
2. 丢弃工作树全部未提交改动：git reset --hard HEAD（已跟踪文件，含暂存区）
   + git clean -fd（未跟踪文件与目录；忽略文件与嵌套 git 仓库不动）
3. 工作树已干净 → 无操作；无法丢弃的残留 → error-dirty
```

`clear` 不联网、不碰主机文件，是一键替代"error-dirty 手动处理"的入口：
对中断残留的 `source/` 镜像改动，push/commit 选择补提交了结（以工作树
当前内容为准），clear 选择直接丢弃（以 HEAD 为准）。

## sync.list 语法

```gitignore
# 默认不管理任何文件；普通行=纳入，!=排除，后规则胜出
# 仅支持绝对路径；** 跨目录段；目录递归写 /path/dir/**
/root/.codeagent/AGENTS.md
/root/.config/opencode/**
!/root/.config/opencode/*.tmp
```

## 运行配置

配置仓库中的 `.sync-config/config.toml`（对所有机器生效）：

```toml
max_backups_per_file = 20     # sync 写回前每个文件保留的历史备份份数（0=不清理）

[git]
name = "sync-bot"
email = "sync-bot@invalid"
```

## 冲突处理

没有合并引擎：只有 `pull` 对本地分支做 `--ff-only` 快进，`push` 在远端领先
或分叉时直接拒绝。由此：

- **同文件双侧修改** —— 拆分后 `commit push` **不再**静默覆盖远端：若远端在你
  commit 期间前进，`push` 会以 `error-conflict`（分叉）停止。想让本机内容胜出：
  先 `pull`，本地取舍后再 `commit push`；本机内容已是本地提交时，直接 `push sync`。
- **分支分叉**（本地与远端各有对方没有的提交，通常是有人在 worktree 手工
  提交）—— `push`/`pull` 以 `error-conflict` 停止并保留现场，交人工：

```bash
cd /var/lib/ai-config-sync/repo
git log --oneline --graph --all          # 查看分叉情况
# 要么手工推送本地提交，要么有意丢弃之，然后重跑 ai-config-sync push / pull sync
```

- **推送被拒（non-fast-forward）** —— 另一台机器恰好在你的 fetch 与 push
  之间推了内容。重跑命令即可：会重新对齐并再次归档。
- **push 时本地落后** —— 远端有本地没有的提交（例如你只 `commit` 而没先 `pull`）。
  `push` 以 `error-conflict` 拒绝并提示 `pull` / `pull sync`，绝不自动快进。

## 卸载

```bash
python3 -m pip uninstall ai-config-sync
# 或连带清理数据目录：
bash deploy/uninstall.sh --purge-data
```

数据目录默认保留；注意工作树中可能有未推送提交，`state/backups/` 保存文件
历史，先推送或归档再清理。

## 已知边界

- 删除只沿 `commit` 方向传播（镜像移除）；`sync` 永不删除本机文件。
- `sync` 不感知远端、`pull` 不感知主机文件：`pull sync` 才是"以远端为准"的完整写回；
  `push` 不重算主机文件、`sync` 不联网：`push sync` 才是"以本地 git 为准"。
- 多机镜像语义：`commit push` 最后推送者获胜；想保留他机归档的文件，先 `pull sync` 再 `commit push`。
- 应用自身会重写的配置文件会造成归档回流，不要纳入清单。
- 所有机器共用同一份清单与配置（无机器差异）。
- 只处理普通文件；软链接跳过并告警。
- 无常驻进程、无任何合并、无崩溃恢复 journal、无冲突暂停标记。
- 凭据文件（token/私钥/API key 等）默认排除、不建议纳入；仓库所有者可显式授权同步
  （私有仓库不是凭据保险箱：明文凭据会进入 Git 历史并复制到每一个 pull sync 的主机）。

## 开发与测试

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -t . -v
```

测试使用本地 bare 仓库模拟远端，覆盖：清单匹配与硬排除、`commit` 本地归档与镜像移除、
`push` 推送与远端领先/分叉拒绝、`sync` 写回与备份、`pull` 仅快进与分叉拒绝、
组合 `commit push` / `push sync` / `pull sync`、异常组合拒绝、清单前缀缺失报错、
格式校验跳过、软链接跳过、CRLF 字节保真、安装分支规则（含 `--force`
孤儿分支与 attach 默认分支）、认证判定边界、备份修剪。

## 许可证

AGPL-3.0-or-later，见 [LICENSE](LICENSE)。
