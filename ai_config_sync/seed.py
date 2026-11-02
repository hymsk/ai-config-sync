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

# [path_map]: 主机路径前缀 -> source/ 下目录（最长前缀匹配，按路径段边界）
# "~" 在每台机器上展开为本机 home：Linux root 为 /root，Windows Git Bash
# 为 C:/Users/<name>，两者都归档到 source/__user_home__/ 下，多机共用同一布局。
# 未命中映射的路径保持恒等（/etc/x <-> source/etc/x）。
# sync.list 规则与软链接声明支持 ~/ 前缀（如 ~/.config/opencode/**）。
[path_map]
"~" = "__user_home__"
"""

SYNC_LIST = """\
# sync.list：纳入清单（默认不管理任何文件）
# 语法：普通行=纳入  !开头=排除  后规则胜出  支持 * 与 **
# 路径须为绝对路径或 ~/ 前缀（~ 展开为本机 home，多机共用清单用 ~/ 写法）；
# 目录递归写 /path/to/dir/** 或 ~/dir/**
# 软链接声明：/link/path -> /target/path（sync 时在本机建立/修正链接；
# 不归档链接内容，链接路径不能被其他规则当文件管理）
# 示例：
# ~/.codeagent/AGENTS.md
# ~/.config/opencode/**
# !~/.config/opencode/*.tmp
# ~/.vimrc -> ~/dotfiles/vimrc
"""

GITATTRIBUTES = (
    "* -text\n"
    "# 字节级保真：不做任何行尾或编码转换。\n"
)

README = """\
# ai-config-stor

ai-config-sync 配置仓库（手动单向归档）。

- `.sync-config/`：运行配置与纳入清单（不下发到主机）
- `source/`：受管文件，路径对应主机绝对路径（/a/b/c <-> source/a/b/c；
  本机 home 下的文件经 [path_map] 的 "~" = "__user_home__" 映射到 source/__user_home__/ 下）

用法（在已安装本工具的机器上手动执行）：
- `ai-config-sync commit push`：把本机受管文件归档到本仓库并推送
- `ai-config-sync push sync`：以本地 config 为准（推送本地提交并写回本机）
- `ai-config-sync pull sync`：拉取远端并写回本机（写前备份）

凭据文件（token/私钥/API key 等）默认排除、不建议纳入；经所有者显式授权后可纳入，
但明文凭据会进入 Git 历史并复制到每一个同步节点，请自行评估并准备轮换。
"""
