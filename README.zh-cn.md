# ai-config-sync

> 通过专用 Git 仓库，让多台 Linux 服务器之间的配置文件保持同步。

多台 Linux 服务器之间的配置同步 Agent：通过一个专用 Git 配置仓库，
每 5 秒一轮自动收集本机受管文件的变化并写回远端更新。发生合并冲突时
**只暂停本机**（在运行用户 Home 下创建空文件 `ERROR_MERGE_FLAG`），
保留 Git 冲突现场等待人工处理；删除标记且复核通过后自动恢复同步。

[English](README.md) · [规范](SPEC.md) · [版本记录](CHANGELOG.md)

- 纯规则驱动，无 LLM、无外部 Python 依赖（Python >= 3.9，自带极简 TOML 解析）。
- 全程不 rebase、不 force push、不 reset、不 clean；冲突永远保留现场。
- 以文件为准：所有变更判定都是"主机文件 Hash <-> Git blob Hash"即时对比，
  不维护 per-file 状态文件；唯一本机状态是追加式事务日志 journal。

## 两个仓库

| 仓库 | 内容 |
|---|---|
| `ai-config-sync`（本仓库） | Agent 程序、种子模板、systemd 单元 |
| 配置仓库（如 `ai-shared-config`） | `.sync-config/`（运行配置与纳入清单）+ `source/`（受管文件镜像） |

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
/var/lib/ai-config-sync/state/       # journal.jsonl / backups/ / sync.log / lock
$HOME/ERROR_MERGE_FLAG               # 冲突暂停标记（唯一 $HOME 依赖）
```

Agent 自身目录被硬排除，任何清单规则都不能把 Agent 自己同步出去。

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

## 首轮采用

```bash
ai-config-sync once                # 仅预览差异（differs/host-only/repo-only/...）
ai-config-sync once --prefer local # 本机为准：收集并推送（覆盖远端版本）
ai-config-sync once --prefer repo  # 仓库为准：备份后写回本机
ai-config-sync once --prefer merge # 三方裁决：双侧不同的文件产生 add/add
                                   # 冲突标记（ours=仓库版 theirs=本机版），
                                   # 逐文件人工解决
```

`--prefer` 只作用于"双侧都有且内容不同"的文件；单侧文件
（仅本机/仅仓库）在任何取值下都向双侧收敛。

## 运行

```bash
ai-config-sync run        # 常驻（每 interval 秒一轮）
ai-config-sync once       # 单轮
ai-config-sync doctor     # 体检：bootstrap/远端/分支/工作树/journal/标记
```

systemd 部署见 `deploy/ai-config-sync.service`。

## 单轮事务（P0-P7）

```text
P0 预检（锁/标记/工作树健康）
P1 读取生效配置与清单展开（稳定读：两次 Hash 一致）
P2 本机 vs 轮始 HEAD（A）归类变更
P3 变更写入工作树 source/... 并提交（仅显式路径，不联网）
P4 fetch + merge origin/<branch>（冲突→置标记保留现场）
P5 push（non-FF → fetch+merge 重试 ≤3）
P6 写回本机：写前复核（本机==A 才写；轮中被改→不覆盖）、
   格式校验、备份、原子替换
P7 journal checkpointed（下轮 A = 当前 HEAD）
```

崩溃恢复：journal 每阶段先 fsync 再执行动作；重启后按最后一条事务记录
恢复（captured→P4、merged→P5、pushed→P6）。journal 损坏时不猜测，
进入 `paused-needs-doctor`，由 `doctor` 辅助人工定基线。

## 冲突处理

**冲突定义从严**：同一文件被双侧（本机与远端）同时修改即视为冲突，强制交人工——即使改动区域不重叠、行级可自动融合。行级融合可能产出任何一侧都不想要的混合配置，因此不做自动融合；单侧修改、双侧修改不同文件仍自动同步。

```bash
git -C /var/lib/ai-config-sync/repo ls-files -u   # 查看冲突路径
# 工作树中该文件为 ours 侧完整内容；两侧内容可分别查看：
git -C /var/lib/ai-config-sync/repo show :2:source/<路径>   # ours
git -C /var/lib/ai-config-sync/repo show :3:source/<路径>   # theirs
# 整文件选边（ours=本机采集版 theirs=远端版；首轮 merge 采用时相反）
git -C /var/lib/ai-config-sync/repo checkout --theirs -- source/<路径>
git -C /var/lib/ai-config-sync/repo add <路径>              # 或编辑后 add
git -C /var/lib/ai-config-sync/repo commit --no-edit
rm ~/ERROR_MERGE_FLAG
```

只删标记不解决冲突会被重新置标记。冲突只暂停本机，其他服务器继续同步。
该保护通过 merge driver（`aics-conflict`）实现，agent 每轮自动确保其在位，
手工 `git merge` 同样受保护。

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
interval = 5                  # 本机检查间隔（秒）
remote_check_interval = 5     # 无本机变化时的远端检查间隔（秒）
max_backups_per_file = 20     # 每个文件保留的写回前备份份数（0=不清理）

[git]
name = "sync-bot"
email = "sync-bot@invalid"
```

## 卸载

```bash
systemctl disable --now ai-config-sync   # 若以 systemd 单元部署
python3 -m pip uninstall ai-config-sync
```

数据目录默认保留；确认无需保留后可执行 `bash deploy/uninstall.sh --purge-data`
删除 `/etc/ai-config-sync` 与 `/var/lib/ai-config-sync`（注意：工作树中可能
有未推送提交，`state/backups/` 保存文件历史，先推送或归档再清理）。

## 已知边界

- 文件删除不自动传播：本机缺失、仓库有 → 下轮重建；远端删除 → 本机保留。
- 应用自身会重写的配置文件会造成同步回流，不要纳入清单。
- 所有机器共用同一份清单与配置（无机器差异）。
- 只处理普通文件；软链接跳过并告警。
- 不要把 token/私钥等凭据放进配置仓库；私有仓库不是凭据保险箱。

## 开发与测试

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -t . -v
```

测试使用本地 bare 仓库模拟远端，覆盖：清单匹配与硬排除、双机同步、
冲突暂停与人工恢复、非快进竞态、journal 断点恢复、轮中修改保护、
删除重建、CRLF 字节保真、安装分支规则与首轮采用、认证判定边界、
备份修剪、首轮采用推送失败恢复。

## 许可证

AGPL-3.0-or-later，见 [LICENSE](LICENSE)。
