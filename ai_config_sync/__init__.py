"""ai-config-sync: 多服务器配置同步 Agent。

通过专用 Git 配置仓库在多台 Linux 服务器之间双向同步受管配置文件；
冲突时仅暂停本机（~/ERROR_MERGE_FLAG），人工处理后恢复。
"""

__version__ = "0.1.0"
