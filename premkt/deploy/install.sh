#!/bin/bash
# 安装/卸载 premkt 的 launchd 定时任务。不会自动执行，需要你手动跑。
#   ./premkt/deploy/install.sh install
#   ./premkt/deploy/install.sh uninstall
#   ./premkt/deploy/install.sh status
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Library/LaunchAgents"
JOBS=(com.premkt.scan com.premkt.evaluate)
CMD="${1:-status}"

case "$CMD" in
  install)
    mkdir -p "$DEST"
    for j in "${JOBS[@]}"; do
      cp "$HERE/$j.plist" "$DEST/$j.plist"
      launchctl bootout "gui/$(id -u)/$j" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$DEST/$j.plist"
      echo "已安装 $j"
    done
    echo "提示：任务只在自检通过且时段匹配时才真正执行，否则静默跳过。"
    ;;
  uninstall)
    for j in "${JOBS[@]}"; do
      launchctl bootout "gui/$(id -u)/$j" 2>/dev/null || true
      rm -f "$DEST/$j.plist"
      echo "已移除 $j"
    done
    ;;
  status)
    for j in "${JOBS[@]}"; do
      if launchctl print "gui/$(id -u)/$j" >/dev/null 2>&1; then
        echo "  $j: 已加载"
      else
        echo "  $j: 未加载"
      fi
    done
    ;;
  *) echo "用法: install.sh {install|uninstall|status}" >&2; exit 64 ;;
esac
