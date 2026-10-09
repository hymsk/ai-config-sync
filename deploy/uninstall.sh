#!/usr/bin/env bash
# 保守卸载：默认只停用 systemd 服务并卸载 Python 包；
# 数据目录（bootstrap、工作树、journal、备份、日志）默认保留，
# 仅在显式传入 --purge-data 且交互确认后删除。
set -euo pipefail

PURGE_DATA=0
for arg in "$@"; do
  case "$arg" in
    --purge-data) PURGE_DATA=1 ;;
    *) echo "未知参数: $arg（仅支持 --purge-data）" >&2; exit 2 ;;
  esac
done

UNIT=/etc/systemd/system/ai-config-sync.service
if [ -e "$UNIT" ] || systemctl list-unit-files ai-config-sync.service >/dev/null 2>&1; then
  systemctl disable --now ai-config-sync 2>/dev/null || true
  echo "已停用并禁用 ai-config-sync 服务（单元文件保留，可手动删除 $UNIT）"
fi

if python3 -m pip show ai-config-sync >/dev/null 2>&1; then
  python3 -m pip uninstall -y ai-config-sync
fi

echo "已卸载 ai-config-sync 程序。"
if [ "$PURGE_DATA" -ne 1 ]; then
  cat <<'EOF'
数据目录已保留：
  /etc/ai-config-sync/        # bootstrap（重新绑定需先删除）
  /var/lib/ai-config-sync/    # 工作树（可能有未推送提交）、journal、backups/、日志
确认不再需要后执行: bash deploy/uninstall.sh --purge-data
EOF
  exit 0
fi

echo "警告: 将删除 /etc/ai-config-sync 与 /var/lib/ai-config-sync（含未推送提交与备份历史）。"
printf "输入 yes 确认删除: "
read -r answer
if [ "$answer" = "yes" ]; then
  rm -rf /etc/ai-config-sync /var/lib/ai-config-sync
  echo "数据目录已删除。"
else
  echo "已取消，数据目录保留。"
  exit 1
fi
