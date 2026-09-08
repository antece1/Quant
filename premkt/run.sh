#!/bin/bash
# premkt 统一入口：先自检，通过才跑，全程留日志。
#
# 为什么要包一层：OpenD 掉线或未登录时，扫描器不会报错，只会输出空表。
# 定时任务里这种"成功地跑出了错误结果"最难发现，所以必须先过 preflight。
#
#   ./premkt/run.sh scan --save
#   ./premkt/run.sh volspike
#   ./premkt/run.sh odte_radar --all
#   REQUIRE_SESSION=regular ./premkt/run.sh dual
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOGDIR="$ROOT/premkt/logs"
mkdir -p "$LOGDIR"

MODULE="${1:-}"
if [[ -z "$MODULE" ]]; then
  echo "用法: run.sh <module> [args...]   例如: run.sh volspike --backfill" >&2
  exit 64
fi
shift

STAMP="$(date +%Y-%m-%d)"
LOG="$LOGDIR/${MODULE}_${STAMP}.log"

{
  echo "=================================================================="
  echo "[$(date '+%F %T %Z')] run.sh $MODULE $*"
} >> "$LOG"

# ---- 自检 ----
# 注意：macOS 自带 bash 3.2，set -u 下展开空数组会报 unbound variable，
# 所以这里不用数组，直接分支。
if [[ -n "${REQUIRE_SESSION:-}" ]]; then
  "$PY" -m premkt.preflight --require-session "$REQUIRE_SESSION" >> "$LOG" 2>&1
else
  "$PY" -m premkt.preflight >> "$LOG" 2>&1
fi
RC=$?
case $RC in
  0) ;;
  1) echo "[$(date '+%F %T')] 中止：OpenD 不可用" | tee -a "$LOG" >&2; exit 1 ;;
  2) echo "[$(date '+%F %T')] 中止：能连上但数据有问题" | tee -a "$LOG" >&2; exit 2 ;;
  3) echo "[$(date '+%F %T')] 跳过：当前不是要求的时段 ($REQUIRE_SESSION)" | tee -a "$LOG"; exit 0 ;;
  *) echo "[$(date '+%F %T')] 中止：preflight 未知返回 $RC" | tee -a "$LOG" >&2; exit $RC ;;
esac

# ---- 正式跑 ----
"$PY" -m "premkt.$MODULE" "$@" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[$(date '+%F %T')] $MODULE 结束 rc=$RC" >> "$LOG"

# 日志保留 30 天
find "$LOGDIR" -name '*.log' -mtime +30 -delete 2>/dev/null
exit $RC
