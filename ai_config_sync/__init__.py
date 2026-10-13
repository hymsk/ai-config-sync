"""ai-config-sync: 配置单向归档工具。

四个原子步骤 + 三个固定组合，手动触发：
- commit：本机文件 → 本地 config（归档 + 本地提交，不联网）
- push：  本地 config → 远端（只推送本地提交）
- sync：  本地 config → 本机（写回，不联网、永不删主机文件）
- pull：  远端 → 本地 config（仅快进，不碰主机文件）
- 组合：commit push / push sync / pull sync（按执行顺序）
无常驻进程、无自动双向收敛。
"""

__version__ = "0.4.0"
