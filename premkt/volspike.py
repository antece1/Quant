"""
买量爆炸提醒器 —— 基于分钟 K 线，不是轮询差分。

为什么不用轮询差分：30 秒轮一次、两次之间做减法，会把爆发那一分钟和
平静的半分钟平均掉，信号被稀释。期权合约和正股都有分钟 K 线（实测
get_cur_kline 可取 400 根），直接读逐分钟成交量才是对的口径，而且
**开机时能回补当天已经发生的所有爆发**，不会因为启动晚了就漏掉。

实测标定（2026-09-01）:
    AAPL 10:22 ET 正股 212,174 股 = 中位量 2.2x，下一分钟 284,260 = 2.9x
         同刻期权 14,576 张 = 2.9x，CALL 12,110 / PUT 2,466
    META 10:37-10:39 ET 期权 4.5x / 4.2x / 3.6x，CALL 占 97%
    -> 3x 中位量是个能抓到真实爆发、又不至于满屏噪声的阈值

两级检测（受订阅额度所迫，也更贴近人的做法）:
    一级  所有票的正股分钟量（便宜，10 只约 1.5 秒）
    二级  只对一级触发的票查 ATM±2 期权的 call/put 拆分
          OpenD 的期权订阅额度只有 20，所以查完即退订

用法:
    python -m premkt.volspike                # 按星期规则选 universe
    python -m premkt.volspike --backfill     # 只回看今天已发生的爆发
    python -m premkt.volspike --mult 2.5 --interval 30
    python -m premkt.volspike --watch US.AAPL US.META
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque

import numpy as np
import pandas as pd
from futu import RET_OK, AuType, KLType, SubType

from .data import _throttle, now_et, quote_ctx, session_label, snapshots
from .fmt import c as _c, lj
from .odte_radar import pick_universe, universe_mode
from .options import expiries

CFG = dict(
    interval=30,
    # 阈值标定（2026-09-01 实测，AAPL/META 各 118 个可判定分钟）:
    #   滚动中位数会自适应局部量能，倍数比"全日中位数"口径压缩很多。
    #   AAPL p99=2.01 全天最大 2.05；META p99=2.53 最大 2.99。
    #   所以 3.0x 全天一次都不会触发 —— 2.0x 才是 p95~p99 的位置。
    mult=2.0,               # 单根尖峰：分钟量 / 滚动中位数
    sustain_mult=1.6,       # 持续放量：较低的门槛
    sustain_n=3,            # 最近 sustain_win 分钟里有几根超过 sustain_mult
    sustain_win=5,
    median_window=30,       # 滚动中位数的窗口（分钟）
    min_bars=12,            # 不足这么多根不判定（开盘前几分钟基数不稳）
    min_turnover=3e5,       # 该分钟成交额下限，滤掉低价小票的假信号
    opt_strikes=2,          # 二级检测取 ATM±N 档
    opt_check_top=2,        # 每轮最多查几只票的期权（订阅额度限 20）
    alert_keep=12,
    index_codes=("US.SPY", "US.QQQ", "US.IWM"),
    index_sync_n=2,         # ±window 内有几个指数 ETF 也在放量就算"大盘同步"
    index_sync_window=1,    # 分钟
    sync_dominance=1.5,     # 个股倍数 >= 指数倍数的这个比例，就算个股独立
    bars_fetch=400,
)


def _pt(t: pd.Timestamp) -> str:
    return (t - pd.Timedelta(hours=3)).strftime("%H:%M")


# ---------------------------------------------------------------------------
def read_minutes(q, code: str, n: int | None = None,
                 closed_only: bool = False) -> pd.DataFrame | None:
    """读某标的今天的分钟 K 线。需要先 subscribe。

    closed_only：丢掉当前这根未走完的分钟。

    为什么必须有这个开关：11:58:37 去读，11:58 这根只有 37 秒的量，判定多半不触发，
    但水位线一推过去，等 11:58 走完、量补齐之后就再也不会被重新检查 —— 爆发被静默漏掉。
    实测就是这么漏的：回补在 11:58 抓到 SPY 3.44x，实时循环却连报 6 轮"无异动"。
    代价是警报最多晚 60 秒，但那是分钟级信号的固有延迟，本来就躲不掉。
    """
    ret, d = q.get_cur_kline(code, n or CFG["bars_fetch"], KLType.K_1M, AuType.QFQ)
    if ret != RET_OK or d is None or len(d) == 0:
        return None
    d = d.copy()
    d["t"] = pd.to_datetime(d["time_key"])
    d = d[d["t"].dt.date == now_et().date()]
    if closed_only and len(d):
        cur = pd.Timestamp(now_et().replace(tzinfo=None)).floor("min")
        d = d[d["t"] < cur]
    return d if len(d) else None


def find_spikes(d: pd.DataFrame, mult: float, after: pd.Timestamp | None = None) -> list[dict]:
    """滚动中位数法找爆发，两种形态:

      spike    单根尖峰，量 >= mult x 滚动中位数
      sustain  持续放量，最近 sustain_win 分钟里有 sustain_n 根 >= sustain_mult

    用中位数而不是均值：均值会被爆发本身推高，连续爆发时后面几根反而测不出来。
    再 shift(1) 只用该分钟之前的历史，避免自我参照。

    持续放量往往比单根尖峰更值得看 —— META 2026-09-01 的 10:35~10:39
    是连续五分钟 2.1~3.0x 的堆量，单看任何一根都不够惊人，合起来才是真正的建仓。
    """
    if d is None or len(d) < CFG["min_bars"]:
        return []
    d = d.sort_values("t").reset_index(drop=True)
    med = d["volume"].rolling(CFG["median_window"], min_periods=CFG["min_bars"]).median().shift(1)
    x = d["volume"] / med
    out = []
    for i, r in d.iterrows():
        xi = x.iloc[i]
        if pd.isna(xi) or r["turnover"] < CFG["min_turnover"]:
            continue
        if after is not None and r["t"] <= after:
            continue
        lo = max(0, i - CFG["sustain_win"] + 1)
        n_hot = int((x.iloc[lo:i + 1] >= CFG["sustain_mult"]).sum())
        kind = None
        if xi >= mult:
            kind = "spike"
        elif n_hot >= CFG["sustain_n"] and xi >= CFG["sustain_mult"]:
            kind = "sustain"
        if kind is None:
            continue
        out.append(dict(t=r["t"], vol=float(r["volume"]), x=float(xi), kind=kind,
                        n_hot=n_hot,
                        turn=float(r["turnover"]), close=float(r["close"]),
                        chg=(r["close"] / r["last_close"] - 1) * 100 if r["last_close"] else np.nan))
    return out


# ---------------------------------------------------------------------------
def mark_market_sync(found: list[tuple]) -> None:
    """标记"大盘同步"的爆发。

    SPY/QQQ/IWM 同一分钟一起放量时，个股的放量多半只是 beta，不是自己的资金在动。
    实测 2026-09-01 11:47~11:52 有 8 只票同时触发 —— 那是大盘层面的事件，
    和 AAPL 10:23 那种个股独立的买量爆炸性质完全不同，混在一起会淹掉真信号。
    """
    idx = CFG["index_codes"]
    # 用 ±1 分钟窗口而不是精确同一分钟：市场级事件不会对齐到同一秒，
    # 实测 QQQ 在 11:51、IWM 在 11:52/11:53，精确匹配几乎命中不到。
    idx_hits = [(sp["t"], sp["x"]) for code, sp in found if code in idx]
    win = pd.Timedelta(minutes=CFG["index_sync_window"])
    for code, sp in found:
        if code in idx:
            sp["sync"] = False
            continue
        near = [x for t, x in idx_hits if abs(t - sp["t"]) <= win]
        if len(near) < CFG["index_sync_n"]:
            sp["sync"] = False
            continue
        # 关键：个股倍数远超指数时不能算 beta。
        # 实测 NVDA 11:59 是 11.80x，同窗口 SPY 3.44x / QQQ 2.11x —— 前一版只数
        # "有几个指数在放量"就把它隐藏了，11 倍量的事件被静默吞掉，是最坏的假阴性。
        sp["sync"] = sp["x"] < CFG["sync_dominance"] * max(near)


def option_split(q, und: str, minute: pd.Timestamp) -> dict | None:
    """二级：查该票 ATM±N 期权在这一分钟的 call/put 成交拆分。

    期权订阅额度只有 20，所以查完立刻退订。
    """
    try:
        spot = float(snapshots(q, [und], quiet=True).iloc[0]["last_price"])
        exps = [x for x in expiries(q, und) if x >= now_et().date()]
        if not exps:
            return None
        _throttle.wait()
        ret, ch = q.get_option_chain(und, start=str(exps[0]), end=str(exps[0]), option_type="ALL")
        if ret != RET_OK or ch is None or len(ch) == 0:
            return None
        ch = ch.copy()
        ch["k"] = ch["strike_price"].astype(float)
        ks = sorted(ch["k"].unique())
        i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot))
        n = CFG["opt_strikes"]
        sub = ch[ch["k"].isin(set(ks[max(0, i - n): i + n + 1]))]
        codes = sub["code"].tolist()
        if not codes:
            return None
        q.subscribe(codes, [SubType.K_1M])
        try:
            cv = pv = ct = pt_ = 0.0
            # 取够根数覆盖到目标分钟：只取 60 根的话，盘中晚些时候回查
            # 早先的爆发会全部落在窗口之外（实测 11:40 查 10:23 直接取不到）
            back = int((now_et().replace(tzinfo=None) - minute.to_pydatetime()).total_seconds() // 60) + 30
            nbars = min(CFG["bars_fetch"], max(60, back))
            for _, row in sub.iterrows():
                b = read_minutes(q, row["code"], nbars)
                if b is None:
                    continue
                hit = b[b["t"] == minute]
                if hit.empty:
                    continue
                v = float(hit.iloc[0]["volume"]); tn = float(hit.iloc[0]["turnover"])
                if row["option_type"] == "CALL":
                    cv += v; ct += tn
                else:
                    pv += v; pt_ += tn
        finally:
            q.unsubscribe(codes, [SubType.K_1M])
        if cv + pv == 0:
            return None
        return dict(call=cv, put=pv, call_turn=ct, put_turn=pt_,
                    cp=cv / pv if pv > 0 else np.inf, expiry=exps[0])
    except Exception as exc:
        # 不要静默吞掉 —— 二级检测失败会让警报少掉方向信息，必须能看见
        print(_c(f"    [期权拆分失败] {und}: {exc}", "yel"))
        return None


# ---------------------------------------------------------------------------
def fmt_alert(code: str, s: dict, opt: dict | None) -> str:
    tick = code.split(".")[-1]
    kind = s.get("kind", "spike")
    tag = _c("尖峰", "red") if kind == "spike" else _c(f"持续{s.get('n_hot', 0)}/5", "yel")
    if s.get("sync"):
        tag += _c(" 大盘同步", "dim")
    line = (f"{_c(s['t'].strftime('%H:%M'), 'bold')} ET / {_pt(s['t'])} PT  "
            f"{_c(lj(tick, 6), 'bold')} {tag} "
            + _c(f"{s['x']:.2f}x", "grn" if s["x"] >= 2.5 else "yel")
            + f"  {s['vol']:,.0f}股  ${s['turn']/1e6:.1f}M  "
            + _c(f"{s['close']:.2f} {s['chg']:+.2f}%", "grn" if s["chg"] > 0 else "red"))
    if opt:
        side = "CALL" if opt["cp"] > 1.5 else ("PUT" if opt["cp"] < 0.67 else "均衡")
        col = "grn" if side == "CALL" else ("red" if side == "PUT" else "dim")
        cp = "inf" if np.isinf(opt["cp"]) else f"{opt['cp']:.1f}"
        line += ("\n" + " " * 24 + _c(f"↳ 期权同刻 {side}", col)
                 + f"  C {opt['call']:,.0f} / P {opt['put']:,.0f} 张  C/P {cp}"
                 + f"  ${(opt['call_turn']+opt['put_turn'])/1e3:.0f}K  到期 {opt['expiry']}")
    return line


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="买量爆炸提醒器（分钟 K 线）")
    p.add_argument("--interval", type=int, default=CFG["interval"])
    p.add_argument("--mult", type=float, default=CFG["mult"], help="几倍中位量算爆发")
    p.add_argument("--backfill", action="store_true", help="只回看今天已发生的爆发后退出")
    p.add_argument("--watch", nargs="*", help="指定监控标的，覆盖星期规则")
    p.add_argument("--no-options", action="store_true", help="跳过二级期权拆分")
    p.add_argument("--show-sync", action="store_true",
                   help="连大盘同步的一起显示（默认只报个股独立的放量）")
    p.add_argument("--cycles", type=int, default=0)
    a = p.parse_args(argv)
    CFG["mult"] = a.mult

    today = now_et().date()
    with quote_ctx() as q:
        if a.watch:
            codes = a.watch
            why = "指定标的"
        else:
            mode, why = universe_mode(today)
            codes = pick_universe(q, mode)
        print(_c(f"\n买量爆炸提醒器   {now_et():%Y-%m-%d %H:%M ET}   {session_label()}", "bold"))
        print(_c(f"universe {len(codes)} 只 —— {why}", "dim"))
        print(_c(f"阈值：分钟成交量 ≥ {CFG['mult']}x 滚动中位数"
                 f"（{CFG['median_window']}分钟窗口）"
                 f" 且 该分钟成交额 ≥ ${CFG['min_turnover']/1e3:.0f}K", "dim"))
        q.subscribe(codes, [SubType.K_1M])

        # ---- 回补：把今天已经发生的爆发全部找出来 ----
        print(_c("\n▼ 今日已发生的爆发（回补）", "bold"))
        seen: dict[str, pd.Timestamp] = {}
        found = []
        for c in codes:
            d = read_minutes(q, c)
            if d is None:
                continue
            sp = find_spikes(d, CFG["mult"])
            seen[c] = d["t"].max()
            for s in sp:
                found.append((c, s))
        found.sort(key=lambda x: x[1]["t"])
        mark_market_sync(found)
        solo = [(c, sp) for c, sp in found if not sp.get("sync")]
        show = found if a.show_sync else solo
        if not show:
            print(_c("  （今天还没有达到阈值的分钟）", "dim"))
        else:
            for c, sp in show[-CFG["alert_keep"]:]:
                print("  " + fmt_alert(c, sp, None))
            n_sync = len(found) - len(solo)
            print(_c(f"  共 {len(found)} 次，其中 {n_sync} 次是大盘同步（已隐藏，--show-sync 可看）；"
                     f"个股独立 {len(solo)} 次，上面显示最近 {min(len(show), CFG['alert_keep'])} 次", "dim"))

        if a.backfill:
            q.unsubscribe(codes, [SubType.K_1M])
            return 0

        # ---- 实时监控 ----
        print(_c(f"\n▶ 开始监控，每 {a.interval}s 检查一次   Ctrl-C 退出", "cyn"))
        alerts: deque = deque(maxlen=CFG["alert_keep"])
        cycle = 0
        try:
            while True:
                cycle += 1
                t0 = time.time()
                fresh = []
                for c in codes:
                    # 只看已走完的分钟，未完成的那根留到下一轮
                    d = read_minutes(q, c, 90, closed_only=True)
                    if d is None:
                        continue
                    sp = find_spikes(d, CFG["mult"], after=seen.get(c))
                    if sp:
                        seen[c] = max(x["t"] for x in sp)
                    else:
                        seen[c] = max(seen.get(c, d["t"].min()), d["t"].max())
                    for s in sp:
                        fresh.append((c, s))
                mark_market_sync(fresh)
                if not a.show_sync:
                    fresh = [(c, sp) for c, sp in fresh if not sp.get("sync")]
                fresh.sort(key=lambda x: -x[1]["x"])
                for idx, (c, s) in enumerate(fresh):
                    opt = None
                    if not a.no_options and idx < CFG["opt_check_top"]:
                        opt = option_split(q, c, s["t"])
                    msg = fmt_alert(c, s, opt)
                    alerts.appendleft(msg)
                    print("\a" + _c("▲ ", "red") + msg, flush=True)
                if not fresh:
                    print(_c(f"  [{now_et():%H:%M:%S}] 第{cycle}轮 无异动 "
                             f"({time.time()-t0:.1f}s)", "dim"), flush=True)
                if a.cycles and cycle >= a.cycles:
                    break
                time.sleep(max(0, a.interval - (time.time() - t0)))
        except KeyboardInterrupt:
            print(_c("\n已停止。", "dim"))
        finally:
            q.unsubscribe(codes, [SubType.K_1M])
    return 0


if __name__ == "__main__":
    sys.exit(main())
