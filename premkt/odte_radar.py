"""
0DTE 期权异动雷达 —— 常驻监控，主排序键是期权流，不是股价动量。

和 dual.py 的问题顺序相反：先问"钱在哪条链上动"，再看正股。
META 2026-08-28 那类机会（股价只涨 2.5%、动量口径平庸，但 0DTE ATM
成交/持仓 16x）只有这个顺序才抓得到。

星期规则（实测确认，不是惯例推断）:
    周一/三/五  单票有当日到期，但只覆盖极少数超大盘 -> MAG7
    周二/四     单票无当日到期，最近到期在明天 -> 1DTE
    周五        周度到期日，覆盖最广 -> 全市场
    SPY/QQQ/IWM 每个交易日都有到期，任何时候都是 0DTE
实现上不写死：每只票取自己"最近的、>= 今天"的到期日，星期只决定 universe 范围。

30 秒一轮的工程约束:
    不可能每轮拉几百只票的完整链。所以拆成
      bootstrap(每天一次) 定 universe + 缓存 ATM 附近的合约代码
      poll(每轮)        只刷这些合约的快照，2~4 次调用搞定
用法:
    python -m premkt.odte_radar                  # 按当天规则自动选 universe
    python -m premkt.odte_radar --once           # 只跑一轮
    python -m premkt.odte_radar --interval 30 --top 20
    python -m premkt.odte_radar --all            # 强制全市场（忽略星期规则）
"""

from __future__ import annotations

import argparse
import datetime as dt

import sys
import time
from collections import defaultdict, deque
import math

import numpy as np
import pandas as pd
from futu import RET_OK

from .data import _Throttle, now_et, quote_ctx, session_label, snapshots
from .fmt import c as _c, lj, rj
from .intraday import full_universe
from .options import expiries

MAG7 = ["US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.NVDA", "US.META", "US.TSLA"]
INDEX_DAILY = ["US.SPY", "US.QQQ", "US.IWM"]   # 每个交易日都有到期

CFG = dict(
    interval=30,
    strikes_each_side=4,       # ATM 上下各取几档
    all_mode_top_n=45,         # 全市场模式下按成交额取前 N 只
    all_mode_min_turnover=2e7,
    top_display=15,
    alert_keep=8,
    # 异动判定
    alert_min_contracts=800,   # 单轮新增成交的绝对下限，滤掉小票噪声
    alert_burst_mult=4.0,      # 单轮新增 >= 基线中位数的几倍
    alert_voi=3.0,             # 全链成交/持仓比达到多少算异动
    alert_spot_move=0.8,       # 单轮正股涨跌幅(%)
    baseline_cycles=10,
    warmup_cycles=3,        # 基线需要几轮才算建立
)

_chain_throttle = _Throttle(max_calls=18, window=30.0)


# ---------------------------------------------------------------------------
def universe_mode(day: dt.date, force_all: bool = False) -> tuple[str, str]:
    """按星期决定 universe 范围。返回 (mode, 说明)。"""
    if force_all:
        return "ALL", "强制全市场（--all）"
    wd = day.weekday()
    if wd in (3, 4):      # 周四(1DTE) / 周五(0DTE)
        return "ALL", "周四=次日到期 / 周五=当日到期，单票覆盖最广 -> 全市场"
    return "MAG7", "周一/二/三 单票到期只覆盖超大盘 -> MAG7 + 指数 ETF"


def pick_universe(q, mode: str) -> list[str]:
    base = list(INDEX_DAILY)
    if mode == "MAG7":
        return base + MAG7
    uni = full_universe(q)
    snap = snapshots(q, uni["code"].tolist())
    d = snap.copy()
    d["to"] = pd.to_numeric(d["turnover"], errors="coerce").fillna(0)
    d = d[d.to >= CFG["all_mode_min_turnover"]].sort_values("to", ascending=False)
    picked = [c for c in d["code"].tolist() if c not in base][: CFG["all_mode_top_n"]]
    return base + picked


# ---------------------------------------------------------------------------
def bootstrap(q, codes: list[str], today: dt.date) -> dict:
    """每天一次：为每只票定最近到期日，并缓存 ATM 附近的合约代码。"""
    spot = {}
    snap = snapshots(q, codes, quiet=True)
    for _, r in snap.iterrows():
        spot[r["code"]] = float(r["last_price"] or 0)

    book = {}
    for code in codes:
        s = spot.get(code, 0)
        if s <= 0:
            continue
        exps = [x for x in expiries(q, code) if x >= today]
        if not exps:
            continue
        exp = exps[0]
        _chain_throttle.wait()
        try:
            ret, ch = q.get_option_chain(code, start=str(exp), end=str(exp), option_type="ALL")
        except Exception:
            continue
        if ret != RET_OK or ch is None or len(ch) == 0:
            continue
        ch = ch.copy()
        ch["k"] = ch["strike_price"].astype(float)
        ks = sorted(ch["k"].unique())
        if not ks:
            continue
        i = int(np.argmin([abs(k - s) for k in ks]))
        n = CFG["strikes_each_side"]
        keep = set(ks[max(0, i - n): i + n + 1])
        sub = ch[ch["k"].isin(keep)]
        book[code] = dict(
            expiry=exp, dte=(exp - today).days, spot0=s,
            contracts=sub["code"].tolist(),
            meta={r["code"]: (float(r["k"]), r["option_type"]) for _, r in sub.iterrows()},
        )
    return book


def poll(q, book: dict) -> pd.DataFrame:
    """每轮：只刷缓存合约 + 正股的快照，聚合成每只票一行。"""
    all_contracts = [c for v in book.values() for c in v["contracts"]]
    if not all_contracts:
        return pd.DataFrame()
    csnap = snapshots(q, all_contracts, quiet=True).set_index("code")
    usnap = snapshots(q, list(book.keys()), quiet=True).set_index("code")

    rows = []
    for code, v in book.items():
        cv = pv = co = po = 0.0
        for cc, (k, otype) in v["meta"].items():
            if cc not in csnap.index:
                continue
            r = csnap.loc[cc]
            vol = float(r.get("volume") or 0)
            oi = float(r.get("option_open_interest") or 0)
            if otype == "CALL":
                cv += vol; co += oi
            else:
                pv += vol; po += oi
        u = usnap.loc[code] if code in usnap.index else None
        spot = float(u["last_price"]) if u is not None else v["spot0"]
        prev = float(u["prev_close_price"]) if u is not None else 0
        rows.append(dict(
            code=code, name=(u["name"] if u is not None else ""),
            expiry=v["expiry"], dte=v["dte"], spot=spot,
            chg=(spot / prev - 1) * 100 if prev > 0 else np.nan,
            call_vol=cv, put_vol=pv, call_oi=co, put_oi=po,
            vol=cv + pv, oi=co + po,
            voi=(cv + pv) / (co + po) if (co + po) > 0 else np.nan,
            cp=cv / pv if pv > 0 else (np.inf if cv > 0 else np.nan),
            net=cv - pv,
        ))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
def _k(v) -> str:
    """紧凑数字：SPY 单日成交上百万张，逗号分隔会把列挤爆。"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    a = abs(v)
    if a >= 1e6:
        return f"{v/1e6:.2f}M"
    if a >= 1e3:
        return f"{v/1e3:.0f}K"
    return f"{v:.0f}"


class Radar:
    def __init__(self):
        self.hist = defaultdict(lambda: deque(maxlen=CFG["baseline_cycles"]))
        self.last = {}
        self.alerts: deque = deque(maxlen=CFG["alert_keep"])
        self.fired = set()
        self.primed = False

    def step(self, cur: pd.DataFrame) -> None:
        """首轮只登记状态不报警 —— 否则一上来每只票都会因为存量比值触发，刷屏且无信息量。"""
        ts = now_et().strftime("%H:%M:%S")
        first = not self.primed
        for _, r in cur.iterrows():
            code = r["code"]
            prev = self.last.get(code)
            if prev is not None:
                dcall = max(0.0, r["call_vol"] - prev["call_vol"])
                dput = max(0.0, r["put_vol"] - prev["put_vol"])
                dspot = (r["spot"] / prev["spot"] - 1) * 100 if prev["spot"] else 0
                # 基线没建起来之前不报警：否则前几轮每只活跃票都会触发，全是噪声
                ready = len(self.hist[code]) >= CFG["warmup_cycles"]
                base = float(np.median(self.hist[code])) if ready else 0.0

                # 单轮成交暴增：必须同时超过绝对下限和自身基线的倍数
                burst = dcall + dput
                if ready and burst >= CFG["alert_min_contracts"] and base > 0 \
                        and burst >= CFG["alert_burst_mult"] * base:
                    side = "CALL" if dcall > dput * 1.5 else ("PUT" if dput > dcall * 1.5 else "双向")
                    col = "grn" if side == "CALL" else ("red" if side == "PUT" else "yel")
                    self.alerts.appendleft(
                        (ts, "burst", code,
                         _c(f"{side} 成交暴增 +{burst:,.0f} 张/轮", col)
                         + f"  (基线 {base:,.0f})  现价 {r['spot']:.2f} {r['chg']:+.1f}%"))
                # 正股单轮急动
                if abs(dspot) >= CFG["alert_spot_move"]:
                    self.alerts.appendleft(
                        (ts, "spot", code,
                         _c(f"正股单轮 {dspot:+.2f}%", "grn" if dspot > 0 else "red")
                         + f"  现价 {r['spot']:.2f}  C/P {r['cp']:.1f}"
                         if np.isfinite(r["cp"]) else f"  现价 {r['spot']:.2f}"))
                self.hist[code].append(burst)

            # 全链成交/持仓比首次突破阈值
            key = (code, "voi")
            if pd.notna(r["voi"]) and r["voi"] >= CFG["alert_voi"] and key not in self.fired:
                self.fired.add(key)
                if first:
                    self.last[code] = dict(call_vol=r["call_vol"], put_vol=r["put_vol"],
                                           spot=r["spot"])
                    continue
                self.alerts.appendleft(
                    (ts, "voi", code,
                     _c(f"全链成交/持仓 {r['voi']:.1f}x", "cyn")
                     + f"  今日新建仓已达存量 {r['voi']:.1f} 倍  C/P {r['cp']:.1f}"
                     if np.isfinite(r["cp"]) else ""))
            self.last[code] = dict(call_vol=r["call_vol"], put_vol=r["put_vol"], spot=r["spot"])
        self.primed = True


# ---------------------------------------------------------------------------
def render(cur: pd.DataFrame, radar: Radar, mode: str, why: str, cycle: int) -> None:
    print("\033[2J\033[H", end="")
    sess = session_label()
    print(_c(f"0DTE 期权异动雷达   {now_et():%Y-%m-%d %H:%M:%S %a} ET   "
             f"第 {cycle} 轮   [{mode}] {len(cur)} 只   {sess}", "bold"))
    print(_c(why, "dim"))

    warm = min(len(h) for h in radar.hist.values()) if radar.hist else 0
    hint = "" if warm >= CFG["warmup_cycles"] else _c(
        f"   [预热中 {warm}/{CFG['warmup_cycles']} 轮，基线建立前不报暴增]", "yel")
    print(_c("\n▲ 异动提示（最新在上）", "bold") + hint)
    if not radar.alerts:
        print(_c("  （暂无）", "dim"))
    else:
        for ts, kind, code, msg in radar.alerts:
            tick = code.split(".")[-1]
            print(f"  {_c(ts, 'dim')}  {_c(lj(tick, 6), 'bold')}  {msg}")

    print(_c("\n按「全链成交/持仓比」排序", "bold"))
    print(_c(lj("代码", 8) + rj("DTE", 5) + rj("现价", 10) + rj("涨幅", 8)
             + rj("C成交", 10) + rj("P成交", 10) + rj("成交/持仓", 11)
             + rj("C/P", 8) + rj("净成交", 11) + lj("  到期", 12), "dim"))
    print(_c("─" * 92, "dim"))
    d = cur.sort_values("voi", ascending=False).head(CFG["top_display"])
    for _, r in d.iterrows():
        cp = "inf" if np.isinf(r["cp"]) else ("n/a" if pd.isna(r["cp"]) else f"{r['cp']:.2f}")
        cpc = "grn" if (np.isfinite(r["cp"]) and r["cp"] >= 2) else (
              "red" if (np.isfinite(r["cp"]) and r["cp"] < 0.5) else "dim")
        voic = "grn" if (pd.notna(r["voi"]) and r["voi"] >= CFG["alert_voi"]) else "dim"
        print(lj(r["code"].split(".")[-1], 8) + rj(f"{r['dte']}", 5)
              + rj(f"{r['spot']:.2f}", 10)
              + rj(_c(f"{r['chg']:+.1f}%", "grn" if r["chg"] > 0 else "red"), 8)
              + rj(_k(r["call_vol"]), 10) + rj(_k(r["put_vol"]), 10)
              + rj(_c("n/a" if pd.isna(r["voi"]) else f"{r['voi']:.2f}x", voic), 11)
              + rj(_c(cp, cpc), 8)
              + rj(_c(("+" if r["net"] > 0 else "") + _k(r["net"]),
                      "grn" if r["net"] > 0 else "red"), 11)
              + lj(f"  {r['expiry']}", 12))
    print(_c("─" * 92, "dim"))
    print(_c("成交/持仓 = ATM±4档的当日成交 ÷ 存量持仓，>1 说明今天新建仓已超存量", "dim"))
    print(_c("注意：成交量看不出买卖方向 —— C/P 高只说明 call 侧在放量，不等于看涨", "yel"))


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="0DTE 期权异动雷达")
    p.add_argument("--interval", type=int, default=CFG["interval"], help="轮询间隔秒")
    p.add_argument("--once", action="store_true", help="只跑一轮")
    p.add_argument("--all", action="store_true", help="强制全市场 universe")
    p.add_argument("--top", type=int, default=CFG["top_display"])
    p.add_argument("--cycles", type=int, default=0, help="跑 N 轮后退出（0=不限）")
    a = p.parse_args(argv)
    CFG["top_display"] = a.top

    today = now_et().date()
    mode, why = universe_mode(today, a.all)
    print(_c(f"\n[bootstrap] {mode} —— {why}", "cyn"))
    with quote_ctx() as q:
        codes = pick_universe(q, mode)
        print(f"  universe {len(codes)} 只，拉链并缓存 ATM±{CFG['strikes_each_side']} 档合约 …")
        book = bootstrap(q, codes, today)
    if not book:
        print(_c("没有可监控的标的（可能是休市或没有到期日）。", "red"))
        return 1
    n_contracts = sum(len(v["contracts"]) for v in book.values())
    dtes = sorted({v["dte"] for v in book.values()})
    print(_c(f"  就绪：{len(book)} 只 / {n_contracts} 个合约 / DTE={dtes}", "grn"))

    radar = Radar()
    cycle = 0
    try:
        while True:
            cycle += 1
            t0 = time.time()
            with quote_ctx() as q:
                cur = poll(q, book)
            if not cur.empty:
                radar.step(cur)
                render(cur, radar, mode, why, cycle)
                print(_c(f"\n本轮耗时 {time.time()-t0:.1f}s，{a.interval}s 后刷新   Ctrl-C 退出", "dim"))
            if a.once or (a.cycles and cycle >= a.cycles):
                break
            time.sleep(max(0, a.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        print(_c("\n已停止。", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
