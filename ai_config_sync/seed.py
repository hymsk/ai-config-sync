"""配置仓库种子内容。

仅 install new 显式使用；运行期绝不读取或回退到这里。
"""

CONFIG_TOML = """\
# ai-config-sync 运行配置（部署后以本仓库为准）
# interval: 本机检查间隔（秒）
interval = 5
# remote_check_interval: 无本机变化时的远端检查间隔（秒）
remote_check_interval = 5
# max_backups_per_file: 写回前每个文件保留的历史备份份数（0=不清理）
max_backups_per_file = 20

[git]
name = "sync-bot"
email = "sync-bot@invalid"
"""

SYNC_LIST = """\
# sync.list：纳入清单（默认不管理任何文件）
# 语法：普通行=纳入  !开头=排除  后规则胜出  支持 * 与 **
# 仅支持绝对路径；目录递归写 /path/to/dir/**
# 示例：
# /root/.codeagent/AGENTS.md
# /root/.config/opencode/**
# !/root/.config/opencode/*.tmp
"""

GITATTRIBUTES = (
    "* -text\n"
    "# 同一文件双侧修改一律强制冲突，不做行级自动融合；\n"
    "# 单侧修改/不同文件不受影响，仍由 git 自动收敛。\n"
    "source/** merge=aics-conflict\n"
    ".sync-config/** merge=aics-conflict\n"
)

README = """\
# ai-shared-config

ai-config-sync 配置仓库。

- `.sync-config/`：运行配置与纳入清单（不下发到主机）
- `source/`：受管文件，路径对应主机绝对路径（/a/b/c <-> source/a/b/c）

冲突处理：在 Agent 工作树中解决冲突并提交后，删除 ~/ERROR_MERGE_FLAG 恢复同步。
不要在本仓库提交凭据、token、私钥。
"""
