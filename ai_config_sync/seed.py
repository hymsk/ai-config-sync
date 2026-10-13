"""配置仓库种子内容。

仅 install new 显式使用；运行期绝不读取或回退到这里。
"""

CONFIG_TOML = """\
# ai-config-sync 运行配置（部署后以本仓库为准）
# max_backups_per_file: pull 写回前每个文件保留的历史备份份数（0=不清理）
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
    "# 字节级保真：不做任何行尾或编码转换。\n"
)

README = """\
# ai-config-stor

ai-config-sync 配置仓库（手动单向归档）。

- `.sync-config/`：运行配置与纳入清单（不下发到主机）
- `source/`：受管文件，路径对应主机绝对路径（/a/b/c <-> source/a/b/c）

用法（在已安装本工具的机器上手动执行）：
- `ai-config-sync commit push`：把本机受管文件归档到本仓库并推送
- `ai-config-sync push sync`：以本地 config 为准（推送本地提交并写回本机）
- `ai-config-sync pull sync`：拉取远端并写回本机（写前备份）

凭据文件（token/私钥/API key 等）默认排除、不建议纳入；经所有者显式授权后可纳入，
但明文凭据会进入 Git 历史并复制到每一个同步节点，请自行评估并准备轮换。
"""
