# ai-config-sync

> 通过专用 Git 仓库，在 Linux 主机之间单向归档与还原配置文件。

一个规则驱动的命令行工具，只有两个显式、手动触发的操作，无常驻进程、
无自动双向收敛、**不做任何合并**：

- `push`（本机 → 仓库）：把本机受管文件归档为配置仓库 `source/` 的精确镜像，
  提交并推送。
- `pull`（仓库 → 本机）：把配置仓库中的受管文件写回本机，写前备份、
  格式校验、原子替换。

[English](README.md) · [规范](SPEC.md) · [版本记录](CHANGELOG.md)

- 纯规则驱动，无 LLM、无外部 Python 依赖（Python >= 3.9，自带极简 TOML 解析）。
- 全程不 rebase、不 force push、不 reset、不 clean、**不做三方合并**：
  本地分支只允许 `--ff-only` 快进对齐远端；真分叉一律报错保留现场交人工。
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
ai-config-sync push   # 本机 → 仓库：归档受管文件，提交并推送
ai-config-sync pull   # 仓库 → 本机：备份后写回受管文件
```

两者都是一次性、手动触发。运行时持有独占锁；第二个实例立即失败，退出码 3。

### push（本机 → 仓库）

```text
1. 取独占锁
2. fetch + 把本地分支**只快进**对齐远端（--ff-only，无任何合并；
   真分叉 → error-conflict，保留现场交人工）
3. 要求工作树干净（有未知改动 → error-dirty）
4. 读取生效配置与清单展开（稳定读：两次 Hash 一致）
5. 前缀检查：每条纳入规则的字面前缀必须存在于本机
   （显式列出的文件没了，或 /etc/xxx/** 的 /etc/xxx 整个不存在
    → error-missing：先改 sync.list 或恢复路径再重跑；
    目录里具体文件被删不算错——走镜像移除）
6. 本机变更写入工作树 source/...
7. 镜像移除：命中清单但本机已不存在的受管路径从仓库 git rm
   （删除沿 push 方向传播）
8. 提交（仅显式路径，不联网），然后 push（被拒=推送竞态 → 报错提示重跑，
   不自动重试）
9. 报告归档/移除文件数
```

镜像语义：`push` 是"最后推送者获胜"。他机归档、而本机没有的文件，push 时
会被**移除**；想保留请先 `pull` 再 `push`。同文件双侧修改**永不冲突**：
本机内容直接覆盖远端（本地优先）；想接受远端版本，同样先 `pull`。

### pull（仓库 → 本机）

```text
1. 取独占锁
2. fetch + 只快进对齐（规则同 push 第 2 步）
3. 读取生效配置与清单展开（含"仓库有、清单命中、本机缺失"的路径——
   这正是 pull 要重建的内容）
4. 对 HEAD 中每个命中清单的 source/... 文件：
   - 本机内容已一致 → 跳过
   - .json/.toml 格式校验失败 → 跳过并计入 skipped
   - 写回前备份到 state/backups/<run_id>/<repo_path>，按配置修剪
   - 原子替换（保留既有 mode/uid/gid；新建 0644）
5. 报告写回/跳过文件数
```

删除不跨过 `pull`：仓库没有的路径，`pull` 永远不会从本机删除。

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
max_backups_per_file = 20     # pull 写回前每个文件保留的历史备份份数（0=不清理）

[git]
name = "sync-bot"
email = "sync-bot@invalid"
```

## 冲突处理

没有合并引擎：`push`/`pull` 对本地分支只做 `--ff-only` 快进。由此：

- **同文件双侧修改** —— `push` 不可能冲突：操作是镜像，本机内容直接覆盖
  远端（本地优先）。想接受远端版本，先执行 `pull`。
- **分支分叉**（本地与远端各有对方没有的提交，通常是有人在 worktree 手工
  提交）—— `push`/`pull` 以 `error-conflict` 停止并保留现场，交人工：

```bash
cd /var/lib/ai-config-sync/repo
git log --oneline --graph --all          # 查看分叉情况
# 要么手工推送本地提交，要么有意丢弃之，然后重跑 ai-config-sync push / pull
```

- **推送被拒（non-fast-forward）** —— 另一台机器恰好在你的 fetch 与 push
  之间推了内容。重跑命令即可：会重新对齐并再次归档。

## 卸载

```bash
python3 -m pip uninstall ai-config-sync
# 或连带清理数据目录：
bash deploy/uninstall.sh --purge-data
```

数据目录默认保留；注意工作树中可能有未推送提交，`state/backups/` 保存文件
历史，先推送或归档再清理。

## 已知边界

- 删除只沿 push 方向传播（镜像移除）；`pull` 永不删除本机文件。
- 多机镜像语义：最后推送者获胜；想保留他机归档的文件，先 `pull` 再 `push`。
- 应用自身会重写的配置文件会造成归档回流，不要纳入清单。
- 所有机器共用同一份清单与配置（无机器差异）。
- 只处理普通文件；软链接跳过并告警。
- 无常驻进程、无任何合并、无崩溃恢复 journal、无冲突暂停标记。
- 凭据文件（token/私钥/API key 等）默认排除、不建议纳入；仓库所有者可显式授权同步
  （私有仓库不是凭据保险箱：明文凭据会进入 Git 历史并复制到每一个 pull 的主机）。

## 开发与测试

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -t . -v
```

测试使用本地 bare 仓库模拟远端，覆盖：清单匹配与硬排除、`push` 归档与推送、
镜像移除、清单前缀缺失报错、快进对齐与分叉报错、`pull` 写回与备份、
格式校验跳过、软链接跳过、CRLF 字节保真、安装分支规则（含 `--force`
孤儿分支与 attach 默认分支）、认证判定边界、备份修剪。

## 许可证

AGPL-3.0-or-later，见 [LICENSE](LICENSE)。
