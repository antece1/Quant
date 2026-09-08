"""
开跑前的健康检查 —— 部署时必须先过这一关。

为什么需要它：OpenD 的失败模式是**静默返回空数据**，不是报错。
行情未登录、订阅额度耗尽、数据停留在上一个交易日 —— 这几种情况下
扫描器照常跑完、照常输出表格，只是内容是错的或空的。
本模块把这些都变成明确的退出码，供 shell / launchd 判断。

退出码:
    0  一切就绪
    1  OpenD 不可用（进程没起、端口不通、未登录）
    2  可连接但数据有问题（快照取不到、数据陈旧）
    3  可用但当前非交易时段（脚本可据此决定跳过还是继续）

用法:
    python -m premkt.preflight            # 人看的完整报告
    python -m premkt.preflight --quiet    # 只给退出码，供脚本用
    python -m premkt.preflight --require-session regular
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time

from futu import RET_OK, KLType, OpenQuoteContext

from .config import RUNTIME
import datetime as dt

from .data import now_et, session_label
from .fmt import c as _c

CHECK_CODES = ["US.SPY", "US.AAPL"]


def _ok(msg):
    print(_c("  ✓ ", "grn") + msg)


def _bad(msg):
    print(_c("  ✗ ", "red") + msg)


def _warn(msg):
    print(_c("  ! ", "yel") + msg)


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def opend_process() -> str | None:
    try:
        out = subprocess.run(["pgrep", "-f", "MacOS/moomoo_OpenD"],
                             capture_output=True, text=True, timeout=5)
        pid = out.stdout.strip().split("\n")[0] if out.stdout.strip() else None
        return pid or None
    except Exception:
        return None


def run_checks(quiet: bool = False, require_session: str | None = None) -> int:
    say = (lambda *a: None) if quiet else print
    say(_c(f"\npremkt 部署自检   {now_et():%Y-%m-%d %H:%M:%S %a} ET", "bold"))

    host, port = RUNTIME["host"], RUNTIME["port"]

    pid = opend_process()
    if pid:
        if not quiet:
            _ok(f"OpenD 进程在跑 (pid {pid})")
    else:
        if not quiet:
            _bad("OpenD 进程没找到 —— 先启动 /Applications/moomoo_OpenD.app")
        return 1

    if not port_open(host, port):
        if not quiet:
            _bad(f"{host}:{port} 不通 —— OpenD 起来了但没在监听，检查它的端口设置")
        return 1
    if not quiet:
        _ok(f"{host}:{port} 可连接")

    try:
        t0 = time.time()
        q = OpenQuoteContext(host=host, port=port)
    except Exception as exc:
        if not quiet:
            _bad(f"建立连接失败: {exc}")
        return 1

    try:
        ret, st = q.get_global_state()
        if ret != RET_OK:
            if not quiet:
                _bad(f"get_global_state 失败: {st}")
            return 1
        if not quiet:
            _ok(f"连接握手 {time.time()-t0:.2f}s   market_us={st.get('market_us')}")

        # 登录态：未登录时所有行情接口返回空，但不报错
        if not st.get("qot_logined"):
            if not quiet:
                _bad("行情未登录 (qot_logined=False) —— OpenD 里重新登录")
            return 1
        if not quiet:
            _ok(f"行情已登录   交易登录={st.get('trd_logined')}")

        ret, sub = q.query_subscription()
        if ret == RET_OK:
            remain = sub.get("remain", 0)
            opt_remain = sub.get("option_remain_quota", 0)
            line = f"订阅额度 剩余 {remain}/100，期权 {opt_remain}/20"
            if not quiet:
                (_ok if remain >= 20 else _warn)(line)
            if remain < 5:
                if not quiet:
                    _warn("额度快满了 —— 有残留订阅没退，重启 OpenD 可清空")

        # 真取一次数据：登录态正常但没行情权限的情况只有这里能发现
        ret, snap = q.get_market_snapshot(CHECK_CODES)
        if ret != RET_OK or snap is None or snap.empty:
            if not quiet:
                _bad(f"快照取不到: {snap}")
            return 2
        row = snap.iloc[0]
        last = float(row["last_price"] or 0)
        if last <= 0:
            if not quiet:
                _bad("快照返回了，但价格是 0 —— 多半是没有该市场的行情权限")
            return 2
        if not quiet:
            _ok(f"快照可用   {row['code']} = {last:.2f}   更新于 {row['update_time']}")

        # 数据新鲜度：快照的 prev_close 应等于最近一根已完成日线的收盘。
        # 对不上说明数据停留在更早的交易日（本项目踩过多次的坑）。
        ret, kl, _ = q.request_history_kline(
            CHECK_CODES[0], start=str(now_et().date() - dt.timedelta(days=10)),
            end=str(now_et().date()), ktype=KLType.K_DAY, max_count=20)
        if ret == RET_OK and kl is not None and len(kl) >= 2:
            snap_prev = float(row["prev_close_price"] or 0)
            kl = kl.copy()
            kl["d"] = kl["time_key"].str[:10]
            done = kl[kl["d"] < str(now_et().date())]
            if len(done) and snap_prev > 0:
                kprev = float(done["close"].iloc[-1])
                drift = abs(snap_prev - kprev) / kprev
                if drift > 0.005:
                    if not quiet:
                        _warn(f"数据可能陈旧：快照 prev_close={snap_prev:.2f} "
                              f"vs 最近日线收盘={kprev:.2f}（{done['d'].iloc[-1]}）")
                elif not quiet:
                    _ok(f"数据新鲜度校验通过（prev_close 对齐 {done['d'].iloc[-1]} 收盘）")
    finally:
        q.close()

    sess = session_label()
    note = {"premarket": "盘前 —— scan 可跑，intraday/volspike 的日内字段还没累积",
            "regular": "盘中 —— 全部工具可用",
            "afterhours": "盘后 —— 日内字段是今日收盘值，evaluate 可跑",
            "overnight": "夜盘 —— 日内字段是最近一个已收盘交易日的",
            "closed": "休市 —— 数据停留在最近一个交易日"}[sess]
    if not quiet:
        (_ok if sess in ("premarket", "regular", "afterhours") else _warn)(f"时段 {sess}：{note}")

    if require_session and sess != require_session:
        if not quiet:
            _warn(f"要求时段 {require_session}，当前 {sess} —— 退出码 3")
        return 3
    if not quiet:
        print(_c("\n就绪。", "grn"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="premkt 部署自检")
    p.add_argument("--quiet", action="store_true", help="不打印，只给退出码")
    p.add_argument("--require-session",
                   choices=["premarket", "regular", "afterhours", "overnight", "closed"],
                   help="要求当前处于某时段，否则退出码 3")
    a = p.parse_args(argv)
    return run_checks(quiet=a.quiet, require_session=a.require_session)


if __name__ == "__main__":
    sys.exit(main())
