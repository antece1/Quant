"""
双口径扫描：动量 + 波动，但两组按各自用途排序。

大市值 -> 做期权：光有波动不够，还要看期权本身值不值得买。
    核心指标是 IV / 实际波动(RV) 的比值 —— IV 低于 RV 说明期权
    相对于这只票真实的日常波动是便宜的，买方占优；IV 远高于 RV
    说明波动已被 price in，买进去就是付溢价。
    再叠加 0DTE 有无、ATM 持仓/成交/点差。

小市值 -> 做 squeeze：要的是逼空燃料。
    小流通盘 + 融券难借（可借量占流通盘极低，或干脆不可融券）
    + 高波动 + 动量。这几项缺一不可，所以用几何平均。

两组都用几何平均而不是加权和 —— 任一维度接近 0，总分就接近 0。
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import numpy as np
import pandas as pd
from futu import RET_OK

from .data import (daily_klines, now_et, quote_ctx, session_label,
                   snapshots)
from .fmt import c as _c, lj, rj, trunc
from .intraday import _momentum_score, full_universe
from .options import _opt_throttle, expiries, target_0dte_date
from .score import _atr

CFG = dict(
    min_price=3.0,
    min_turnover=5_000_000.0,
    min_change=2.0,
    min_amplitude=2.5,          # 今日振幅下限 —— "波动最大"的入门线
    min_range_pos=0.50,
    split_b=10.0,
    # 盘前时段的门槛要单独一套：盘前成交额和振幅天然比盘中小一个量级
    pre_min_turnover=500_000.0,
    pre_min_amplitude=0.4,
    prelim_per_bucket=40,       # 放宽：上一轮 META 在预选阶段就被砍，期权异动信号没机会评估
    opt_check_n=25,             # 查期权的只数同步放宽
    final_n=10,
)


# ---------------------------------------------------------------------------
def _vol_metrics(k: pd.DataFrame, price: float, today: dt.date) -> dict:
    """ATR14% 与 20 日已实现波动率（年化）。剔除今天这根，避免自我指涉。"""
    out = dict(atr_pct=np.nan, rvol=np.nan)
    if k is None or len(k) < 21:
        return out
    k = k.copy()
    k["_d"] = pd.to_datetime(k["time_key"]).dt.date
    k = k[k["_d"] <= today]
    if len(k) < 21:
        return out
    atr = _atr(k, 14)
    if not math.isnan(atr) and price > 0:
        out["atr_pct"] = atr / price * 100
    c = k["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna().tail(20)
    if len(r) >= 10:
        out["rvol"] = float(r.std(ddof=1) * math.sqrt(252) * 100)
    return out


def _vol_score(amplitude: float, atr_pct: float, rvol: float,
               premkt: bool = False) -> float:
    """波动分：当场振幅 + ATR% + 已实现波动率。

    盘前模式下把权重压到 ATR%/RV 上 —— 盘前区间天然窄且成交稀疏，
    用它当"波动最大"的主要依据会把真正高波动的票排在后面。
    """
    parts, wts = [], []
    amp_w, amp_div = (0.15, 4.0) if premkt else (0.40, 8.0)
    if not math.isnan(amplitude):
        parts.append(min(1.0, amplitude / amp_div)); wts.append(amp_w)
    if not math.isnan(atr_pct):
        parts.append(min(1.0, atr_pct / 6.0)); wts.append(0.50 if premkt else 0.35)
    if not math.isnan(rvol):
        parts.append(min(1.0, rvol / 80.0)); wts.append(0.35 if premkt else 0.25)
    return float(np.average(parts, weights=wts)) if parts else 0.0


# ---------------------------------------------------------------------------
def squeeze_fuel_us(float_shares: float, volume: float) -> tuple[float, float]:
    """美股逼空燃料 = 小流通盘 × 高换手倍数，返回 (分数, 换手倍数)。

    注意：futu 的 short_available_volume / enable_short_sell 是**港股专用**，
    美股全部返回 NaN（NVDA/TSLA/GME 实测均为空）。所以借券难度这一维在
    美股拿不到，只能用「今日成交量 / 流通股本」代理 —— 一天换手掉大半个
    流通盘，本身就说明浮筹在被快速吸收。
    """
    fs, vol = float_shares, volume
    if not (fs and fs > 0):
        return 0.15, float("nan")
    ft = vol / fs                       # 换手倍数
    ft_score = min(1.0, max(0.0, math.log10(max(ft, 1e-4) / 0.01) / math.log10(100)))
    if fs <= 10e6:      fb = 1.00
    elif fs <= 25e6:    fb = 0.85
    elif fs <= 75e6:    fb = 0.60
    elif fs <= 200e6:   fb = 0.35
    else:               fb = 0.15
    return float(0.45 * fb + 0.55 * ft_score), ft


def option_metrics(q, code: str, target: dt.date, spot: float) -> dict:
    """最近到期日 / 是否 0DTE / ATM 隐波 / 持仓 / 成交 / 点差。"""
    out = dict(has_opt=False, has_0dte=False, near_exp=None, dte=None,
               iv=np.nan, oi=np.nan, ovol=np.nan, ospread=np.nan, strike=np.nan)
    exps = expiries(q, code)
    if not exps:
        return out
    out["has_opt"] = True
    fut = [x for x in exps if x >= target]
    if not fut:
        return out
    out["has_0dte"] = target in exps
    out["near_exp"] = fut[0]
    out["dte"] = (fut[0] - target).days

    _opt_throttle.wait()
    try:
        ret, chain = q.get_option_chain(code, start=fut[0].strftime("%Y-%m-%d"),
                                        end=fut[0].strftime("%Y-%m-%d"),
                                        option_type="CALL")
    except Exception:
        return out
    if ret != RET_OK or chain is None or len(chain) == 0:
        return out
    ch = chain.copy()
    ch["_k"] = ch["strike_price"].astype(float)
    row = ch.iloc[(ch["_k"] - spot).abs().argsort().iloc[0]]

    snap = snapshots(q, [row["code"]], quiet=True)
    if snap.empty:
        return out
    s = snap.iloc[0]
    ask, bid = float(s.get("ask_price") or 0), float(s.get("bid_price") or 0)
    mid = (ask + bid) / 2
    _sp = (ask - bid) / mid * 100 if mid > 0 else np.nan
    _iv = float(s.get("option_implied_volatility") or 0) or np.nan
    if not math.isnan(_sp) and _sp > 50:
        _iv = np.nan          # 点差 >50% 说明一边几乎没报价，IV 不可信
    out.update(strike=float(row["_k"]), iv=_iv,
               oi=float(s.get("option_open_interest") or 0),
               ovol=float(s.get("volume") or 0),
               ospread=_sp)
    return out


def vol_oi_ratio(ovol: float, oi: float) -> float:
    """当日成交 / 存量持仓。

    这是异动资金信号，不是流动性指标 —— 比值远大于 1 说明当天有大批**新仓**
    在这个行权价上建立（META 2026-08-28: 成交 17,942 / 持仓 1,095 = 16.4x）。
    持仓为 0 但成交很大，是当天新挂出的行权价，同样算强信号而非流动性差。
    """
    if math.isnan(ovol) or ovol <= 0:
        return np.nan
    if math.isnan(oi) or oi <= 0:
        return 20.0 if ovol >= 500 else np.nan     # 新行权价，按强信号计
    return ovol / oi


def _option_adj(m: dict, rvol: float) -> tuple[float, float, float]:
    """返回 (期权可交易性调整, IV/RV 比值, 量/持仓比)。

    IV/RV < 1 表示期权比这只票真实的波动便宜 —— 买方占优。
    量/持仓 > 5 表示当天有新仓在大举建立 —— 异动资金信号。
    """
    iv, ratio = m.get("iv", np.nan), np.nan
    if not math.isnan(iv) and not math.isnan(rvol) and rvol > 0:
        ratio = iv / rvol

    adj = 1.0
    if math.isnan(ratio):
        # 期权口径下 IV 拿不到（多半是点差过宽、一边没报价）本身就是负面信息
        adj *= 0.55
    else:
        adj *= max(0.55, min(1.20, 1.35 - 0.45 * ratio))

    oi, ov = m.get("oi", np.nan), m.get("ovol", np.nan)
    voi = vol_oi_ratio(ov, oi)
    # 异动加成：当日新建仓远超存量
    if not math.isnan(voi):
        adj *= 1.25 if voi >= 10 else (1.15 if voi >= 5 else (1.06 if voi >= 2 else 1.0))
    # 绝对成交量仍作流动性下限；持仓为 0 时不再按"流动性差"处罚（见 vol_oi_ratio）
    if not math.isnan(ov):
        adj *= 1.0 if ov >= 500 else (0.93 if ov >= 100 else 0.82)
    if not math.isnan(oi) and oi > 0:
        adj *= 1.0 if oi >= 1000 else (0.96 if oi >= 200 else 0.88)
    sp = m.get("ospread", np.nan)
    if not math.isnan(sp):
        adj *= 1.0 if sp <= 5 else (0.9 if sp <= 12 else 0.72)
    if m.get("has_0dte"):
        adj *= 1.10
    return adj, ratio, voi


# ---------------------------------------------------------------------------
def pick_mode(mode: str = "auto") -> tuple[bool, str]:
    """决定用盘前字段还是日内字段，返回 (是否盘前口径, 说明)。

    关键防呆：开盘头 20 分钟日内字段几乎是空的（实测 09:31 时 SPY 振幅
    只有 0.112%），此时按日内口径排"波动最大"完全没有意义 ——
    自动回退到盘前口径，盘前那一场是完整的。
    """
    sess = session_label()
    if mode == "premarket":
        return True, "强制盘前口径"
    if mode == "intraday":
        return False, "强制日内口径"
    if sess == "premarket":
        return True, "盘前时段"
    if sess == "regular" and now_et().time() < dt.time(9, 50):
        return True, "开盘不足 20 分钟，日内字段尚未累积 —— 自动回退到盘前口径"
    return False, f"{sess} 时段"


def run(mode: str = "auto") -> tuple[pd.DataFrame, pd.DataFrame, dt.date]:
    today = now_et().date()
    premkt, why = pick_mode(mode)
    print(_c(f"  口径: {'盘前' if premkt else '日内'}（{why}）", "dim"))
    with quote_ctx() as q:
        print(_c("\n[1/5] 全市场快照 …", "cyn"))
        uni = full_universe(q)
        snap = snapshots(q, uni["code"].tolist())
        d = uni.merge(snap, on="code", how="inner", suffixes=("_f", ""))

        rows = []
        for _, r in d.iterrows():
            mc = float(r.get("total_market_val") or 0)
            if mc <= 0:
                continue
            if premkt:
                # 盘前：必须用 API 自带的 pre_change_rate。
                # prev_close_price 在盘前会滞后一个交易日（实测 8/28 盘前它给的是
                # 周三收盘），自己用 pre_price/prev_close_price 算会得到完全错误的涨幅。
                px = float(r.get("pre_price") or 0)
                hi = float(r.get("pre_high_price") or 0)
                lo = float(r.get("pre_low_price") or 0)
                to = float(r.get("pre_turnover") or 0)
                vol = float(r.get("pre_volume") or 0)
                chg = float(r.get("pre_change_rate") or 0)
                amp = float(r.get("pre_amplitude") or 0)
                vwap_prem = 0.0            # 盘前没有 VWAP
            else:
                px = float(r.get("last_price") or 0)
                prev = float(r.get("prev_close_price") or 0)
                hi = float(r.get("high_price") or 0)
                lo = float(r.get("low_price") or 0)
                vwap = float(r.get("avg_price") or 0)
                to = float(r.get("turnover") or 0)
                vol = float(r.get("volume") or 0)
                if not (prev > 0 and vwap > 0):
                    continue
                chg = (px / prev - 1) * 100
                amp = float(r.get("amplitude") or 0)
                vwap_prem = (px / vwap - 1) * 100
            if not (px > 0 and hi > lo):
                continue
            rows.append(dict(
                code=r["code"], name=r.get("name", ""), price=px,
                change_pct=chg, turnover=to, market_cap=mc, amplitude=amp,
                range_pos=(px - lo) / (hi - lo), vwap_premium=vwap_prem,
                volume=vol,
                float_shares=float(r.get("outstanding_shares") or np.nan),
            ))
        w = pd.DataFrame(rows)
        n0 = len(w)
        min_to = CFG["pre_min_turnover"] if premkt else CFG["min_turnover"]
        min_amp = CFG["pre_min_amplitude"] if premkt else CFG["min_amplitude"]
        cond = ((w.price >= CFG["min_price"]) & (w.turnover >= min_to)
                & (w.change_pct >= CFG["min_change"]) & (w.amplitude >= min_amp)
                & (w.range_pos >= CFG["min_range_pos"]))
        if not premkt:
            cond &= (w.vwap_premium > 0)
        w = w[cond].copy()
        tag = "盘前" if premkt else "日内"
        print(f"      过门槛 {len(w)} / {n0}  [{tag}口径] "
              f"(涨幅≥{CFG['min_change']}% 且 振幅≥{min_amp}% "
              f"{'' if premkt else '且 站上VWAP '}且 区间位置≥{CFG['min_range_pos']:.0%} "
              f"且 成交额≥${min_to/1e6:.2f}M)")
        if w.empty:
            return pd.DataFrame(), pd.DataFrame(), today

        w["bucket"] = np.where(w.market_cap >= CFG["split_b"] * 1e9, "LARGE", "SMALL")
        w["prelim"] = [_momentum_score(r.change_pct, r.range_pos, r.vwap_premium)
                       * min(1.0, r.amplitude / 8.0) for r in w.itertuples()]
        pre = (w.groupby("bucket", group_keys=False)
                .apply(lambda g: g.sort_values("prelim", ascending=False)
                                  .head(CFG["prelim_per_bucket"]))
                .reset_index(drop=True))
        print(f"      分桶预选 大{sum(pre.bucket=='LARGE')} / 小{sum(pre.bucket=='SMALL')}")

        print(_c(f"[2/5] 拉 {len(pre)} 只日线（ATR% / 已实现波动率）…", "cyn"))
        kl = daily_klines(q, pre["code"].tolist())
        vm = [_vol_metrics(kl.get(c), p, today) for c, p in zip(pre["code"], pre["price"])]
        pre = pd.concat([pre.reset_index(drop=True), pd.DataFrame(vm)], axis=1)
        pre["vol_score"] = [_vol_score(r.amplitude, r.atr_pct, r.rvol, premkt)
                            for r in pre.itertuples()]
        pre["mom_score"] = [_momentum_score(r.change_pct, r.range_pos, r.vwap_premium)
                            for r in pre.itertuples()]

        target = target_0dte_date(q)
        print(_c(f"[3/5] 大市值查期权（0DTE 目标日 {target}）…", "cyn"))
        large = pre[pre.bucket == "LARGE"].sort_values(
            ["mom_score", "vol_score"], ascending=False).head(CFG["opt_check_n"]).copy()
        om = [option_metrics(q, r.code, target, r.price) for r in large.itertuples()]
        large = pd.concat([large.reset_index(drop=True), pd.DataFrame(om)], axis=1)

    print(_c("[4/5] 大市值打分（动量 × 波动 × 期权可交易性）…", "cyn"))
    adjs, ratios, vois = [], [], []
    for r in large.itertuples():
        a, ratio, voi = _option_adj(
            dict(iv=r.iv, oi=r.oi, ovol=r.ovol, ospread=r.ospread, has_0dte=r.has_0dte),
            r.rvol)
        adjs.append(a); ratios.append(ratio); vois.append(voi)
    large["opt_adj"] = adjs
    large["iv_rv"] = ratios
    large["vol_oi"] = vois
    large["score"] = [round(100 * math.sqrt(max(m, 0) * max(v, 0)) * a, 1)
                      for m, v, a in zip(large.mom_score, large.vol_score, large.opt_adj)]
    large = large.sort_values("score", ascending=False).head(CFG["final_n"]).reset_index(drop=True)

    print(_c("[5/5] 小市值打分（动量 × 波动 × 逼空燃料）…", "cyn"))
    small = pre[pre.bucket == "SMALL"].copy()
    sq = [squeeze_fuel_us(r.float_shares, r.volume) for r in small.itertuples()]
    small["squeeze_score"] = [x[0] for x in sq]
    small["float_turn"] = [x[1] for x in sq]
    small["score"] = [round(100 * (max(m, 0) * max(v, 0) * max(s, 1e-6)) ** (1 / 3), 1)
                      for m, v, s in zip(small["mom_score"], small["vol_score"], small["squeeze_score"])]
    small = small.sort_values("score", ascending=False).head(CFG["final_n"]).reset_index(drop=True)
    return large, small, target


# ---------------------------------------------------------------------------
def _m(v, unit=True) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or np.isnan(v))):
        return "n/a"
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= d:
            return f"{v/d:.1f}{u}"
    return f"{v:.0f}"


def print_large(df: pd.DataFrame, target: dt.date) -> None:
    print(_c(f"\n大市值 ≥ ${CFG['split_b']:.0f}B · 做期权口径"
             f"（动量 × 波动 × 期权可交易性）", "bold"))
    if df.empty:
        print(_c("  （无）", "yel")); return
    print(_c(lj("#", 4) + lj("代码", 10) + lj("名称", 20) + rj("现价", 9) + rj("涨幅", 8)
             + rj("振幅", 7) + rj("ATR%", 7) + rj("RV", 7) + rj("IV", 7) + rj("IV/RV", 7)
             + rj("0DTE", 8) + rj("到期", 10) + rj("ATM量", 8) + rj("量/持仓", 9)
             + rj("点差", 7) + rj("评分", 7), "bold"))
    print(_c("─" * 142, "dim"))
    for i, r in df.iterrows():
        ratio = r["iv_rv"]
        rc = "grn" if (pd.notna(ratio) and ratio < 1.0) else ("yel" if pd.notna(ratio) and ratio < 1.5 else "red")
        od = _c("✓", "grn") if r["has_0dte"] else _c("✗", "dim")
        print(lj(i + 1, 4) + lj(r["code"], 10) + lj(trunc(r["name"], 18), 20)
              + rj(f"{r['price']:.2f}", 9) + rj(_c(f"{r['change_pct']:+.1f}%", "grn"), 8)
              + rj(f"{r['amplitude']:.1f}%", 7)
              + rj("n/a" if pd.isna(r["atr_pct"]) else f"{r['atr_pct']:.1f}%", 7)
              + rj("n/a" if pd.isna(r["rvol"]) else f"{r['rvol']:.0f}", 7)
              + rj("n/a" if pd.isna(r["iv"]) else f"{r['iv']:.0f}", 7)
              + rj(_c("n/a" if pd.isna(ratio) else f"{ratio:.2f}", rc), 7)
              + rj(od, 8) + rj(str(r["near_exp"] or "—")[5:], 10)
              + rj(_m(r["ovol"]), 8)
              + rj(_c("n/a" if pd.isna(r["vol_oi"]) else f"{r['vol_oi']:.1f}x",
                      "grn" if (pd.notna(r["vol_oi"]) and r["vol_oi"] >= 5) else "dim"), 9)
              + rj("n/a" if pd.isna(r["ospread"]) else f"{r['ospread']:.0f}%", 7)
              + rj(_c(f"{r['score']:.1f}", "grn" if r["score"] >= 55 else "yel"), 7))
    print(_c("─" * 142, "dim"))
    print(_c("RV=20日已实现波动率(年化%)  IV=最近到期 ATM CALL 隐波  "
             "IV/RV<1 = 期权比这只票真实波动便宜，买方占优", "dim"))
    print(_c("量/持仓 = 当日成交 / 存量持仓，≥5x 说明当天在大举建新仓（异动资金信号，不是流动性指标）", "dim"))


def print_small(df: pd.DataFrame) -> None:
    print(_c(f"\n小市值 < ${CFG['split_b']:.0f}B · 做 squeeze 口径"
             f"（动量 × 波动 × 逼空燃料）", "bold"))
    if df.empty:
        print(_c("  （无）", "yel")); return
    print(_c(lj("#", 4) + lj("代码", 10) + lj("名称", 20) + rj("现价", 9) + rj("涨幅", 8)
             + rj("振幅", 7) + rj("ATR%", 7) + rj("RV", 7) + rj("市值", 8)
             + rj("流通盘", 9) + rj("换手倍数", 10)
             + rj("逼空分", 8) + rj("评分", 7), "bold"))
    print(_c("─" * 132, "dim"))
    for i, r in df.iterrows():
        ft = r["float_turn"]
        bc = "grn" if (pd.notna(ft) and ft >= 0.5) else "dim"
        print(lj(i + 1, 4) + lj(r["code"], 10) + lj(trunc(r["name"], 18), 20)
              + rj(f"{r['price']:.2f}", 9) + rj(_c(f"{r['change_pct']:+.1f}%", "grn"), 8)
              + rj(f"{r['amplitude']:.1f}%", 7)
              + rj("n/a" if pd.isna(r["atr_pct"]) else f"{r['atr_pct']:.1f}%", 7)
              + rj("n/a" if pd.isna(r["rvol"]) else f"{r['rvol']:.0f}", 7)
              + rj(_m(r["market_cap"]), 8) + rj(_m(r["float_shares"]), 9)
              + rj(_c("n/a" if pd.isna(ft) else f"{ft:.2f}x", bc), 10)
              + rj(f"{r['squeeze_score']:.2f}", 8)
              + rj(_c(f"{r['score']:.1f}", "grn" if r["score"] >= 55 else "yel"), 7))
    print(_c("─" * 132, "dim"))
    print(_c("换手倍数 = 今日成交量 / 流通股本；≥0.5x 表示半个流通盘一天内易手，浮筹被快速吸收", "dim"))
    print(_c("注意：futu 的融券可得量(short_available_volume)是港股专用字段，美股全部为空，"
             "所以借券难度这一维拿不到 —— 逼空分只由流通盘与换手构成", "yel"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="动量+波动双口径扫描")
    ap.add_argument("--mode", choices=["auto", "premarket", "intraday"], default="auto",
                    help="数据口径。auto 会在开盘头 20 分钟自动用盘前字段")
    a = ap.parse_args(argv)
    sess = session_label()
    note = {"regular": "✓ 盘中，数据实时",
            "premarket": "✓ 盘前口径 —— 用 pre_* 字段（日内字段此时仍是上一场的）",
            "afterhours": "✓ 盘后 —— 日内字段是今日完整收盘数据",
            "overnight": "✓ 夜盘 —— 日内字段是最近一个已收盘交易日的完整数据",
            "closed": "⚠ 休市 —— 日内字段停留在最近一个交易日"}[sess]
    print(_c(f"\n动量+波动双口径  {now_et():%Y-%m-%d %H:%M ET}  ", "bold") + _c(note, "grn"))
    large, small, target = run(a.mode)
    print_large(large, target)
    print_small(small)
    print(_c(f"\n0DTE 指 {target} 当天到期。单票期权是周一/三/五到期，"
             f"周二/四只有 SPY/QQQ/IWM 这类每日到期的指数 ETF 有 0DTE。", "dim"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
