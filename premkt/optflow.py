"""
期权流分析 —— 主排序键是资金流，不是股价动量。

和 dual.py 的顺序完全相反：dual 先问"谁涨得猛"，再问"期权好不好做"；
这里先问"钱在哪个行权价上动"，股价形态只作背景。
META 2026-08-28 那类机会（股价只涨 2.5%、按动量口径平庸，但 0DTE ATM
成交/持仓 16x）只有这个顺序才抓得到。

核心指标:
  Call/Put Wall   持仓量最大的行权价。做市商在那里累积了最多空头合约，
                  对冲盘会把价格往那儿吸（pin），突破则触发追买/追卖。
  成交/持仓比      当日成交 ÷ 存量持仓。远大于 1 = 今天在建新仓，
                  这是"现在正在发生"的信号；纯看 OI 只能看到昨天的存量。
  GEX             gamma × OI，call 为正、put 为负（做市商视角）。
                  正 GEX 区做市商做多 gamma -> 抑制波动；负 GEX 区放大波动。
                  符号翻转的位置就是 gamma flip。

用法:
    python -m premkt.optflow US.TSLA
    python -m premkt.optflow US.TSLA --expiry 2026-09-04
    python -m premkt.optflow US.TSLA --width 15      # 只看 spot ±15%
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import numpy as np
import pandas as pd
from futu import RET_OK

from .data import now_et, quote_ctx, snapshots
from .fmt import c as _c, lj, rj
from .options import _opt_throttle, expiries, target_0dte_date


def _f(v, d=np.nan):
    try:
        x = float(v)
        return d if math.isnan(x) else x
    except (TypeError, ValueError):
        return d


def load_chain(q, code: str, expiry: dt.date) -> tuple[pd.DataFrame, float]:
    """拉一个到期日的完整链并逐合约取快照，返回 (逐合约表, 正股现价)。"""
    spot = _f(snapshots(q, [code], quiet=True).iloc[0]["last_price"])
    _opt_throttle.wait()
    ret, ch = q.get_option_chain(code, start=str(expiry), end=str(expiry), option_type="ALL")
    if ret != RET_OK or ch is None or len(ch) == 0:
        raise RuntimeError(f"get_option_chain failed: {ch}")
    snap = snapshots(q, ch["code"].tolist(), quiet=True)
    d = ch[["code", "strike_price", "option_type"]].merge(snap, on="code", how="inner",
                                                          suffixes=("", "_s"))
    d["strike"] = d["strike_price"].astype(float)
    d["oi"] = d["option_open_interest"].apply(_f).fillna(0)
    d["vol"] = d["volume"].apply(_f).fillna(0)
    d["gamma"] = d["option_gamma"].apply(_f)
    d["delta"] = d["option_delta"].apply(_f)
    d["iv"] = d["option_implied_volatility"].apply(_f)
    d["bid"] = d["bid_price"].apply(_f)
    d["ask"] = d["ask_price"].apply(_f)
    return d, spot


def by_strike(d: pd.DataFrame, spot: float, width_pct: float) -> pd.DataFrame:
    """按行权价汇总 call/put 的持仓、成交、成交持仓比与 GEX。"""
    lo, hi = spot * (1 - width_pct / 100), spot * (1 + width_pct / 100)
    d = d[(d.strike >= lo) & (d.strike <= hi)]
    c = d[d.option_type == "CALL"].set_index("strike")
    p = d[d.option_type == "PUT"].set_index("strike")
    strikes = sorted(set(c.index) | set(p.index))

    rows = []
    for k in strikes:
        cr = c.loc[k] if k in c.index else None
        pr = p.loc[k] if k in p.index else None
        c_oi = float(cr["oi"]) if cr is not None else 0.0
        p_oi = float(pr["oi"]) if pr is not None else 0.0
        c_v = float(cr["vol"]) if cr is not None else 0.0
        p_v = float(pr["vol"]) if pr is not None else 0.0
        c_g = _f(cr["gamma"]) if cr is not None else np.nan
        p_g = _f(pr["gamma"]) if pr is not None else np.nan
        # 做市商视角：客户买 call 则做市商空 call（正 gamma 敞口），put 相反
        gex = 0.0
        if not math.isnan(c_g):
            gex += c_g * c_oi
        if not math.isnan(p_g):
            gex -= p_g * p_oi
        gex *= 100 * spot * spot * 0.01
        rows.append(dict(
            strike=k, call_oi=c_oi, put_oi=p_oi, call_vol=c_v, put_vol=p_v,
            oi=c_oi + p_oi, vol=c_v + p_v,
            call_voi=c_v / c_oi if c_oi > 0 else (np.nan if c_v == 0 else np.inf),
            put_voi=p_v / p_oi if p_oi > 0 else (np.nan if p_v == 0 else np.inf),
            cp_vol_ratio=c_v / p_v if p_v > 0 else (np.nan if c_v == 0 else np.inf),
            cp_oi_ratio=c_oi / p_oi if p_oi > 0 else (np.nan if c_oi == 0 else np.inf),
            net_vol=c_v - p_v,
            gex=gex,
            call_iv=_f(cr["iv"]) if cr is not None else np.nan,
        ))
    return pd.DataFrame(rows)


def gamma_flip(t: pd.DataFrame) -> float:
    """GEX 累计和由负转正的行权价 —— 该点之下做市商放大波动，之上抑制波动。"""
    if t.empty:
        return float("nan")
    cum = t.sort_values("strike")["gex"].cumsum().values
    ks = t.sort_values("strike")["strike"].values
    for i in range(1, len(cum)):
        if cum[i - 1] < 0 <= cum[i]:
            if cum[i] != cum[i - 1]:
                w = -cum[i - 1] / (cum[i] - cum[i - 1])
                return float(ks[i - 1] + w * (ks[i] - ks[i - 1]))
            return float(ks[i])
    return float("nan")


def _n(v, nd=0):
    if v is None or (isinstance(float, type(v)) and math.isnan(v)):
        return "n/a"
    if isinstance(v, float) and (math.isnan(v)):
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "新仓"
    for u, dv in (("M", 1e6), ("K", 1e3)):
        if abs(v) >= dv:
            return f"{v/dv:.1f}{u}"
    return f"{v:.{nd}f}"


def report(code: str, expiry: dt.date, t: pd.DataFrame, spot: float, dte: int) -> None:
    if t.empty:
        print(_c("链内没有可用合约。", "yel")); return
    tot_cv, tot_pv = t.call_vol.sum(), t.put_vol.sum()
    tot_co, tot_po = t.call_oi.sum(), t.put_oi.sum()
    call_wall = t.loc[t.call_oi.idxmax()]
    put_wall = t.loc[t.put_oi.idxmax()]
    # 成交量墙：今天钱堆在哪个行权价。0DTE 用它比持仓墙靠谱得多 ——
    # 持仓墙常常是远月滚仓留下的存量，离现价太远，当天根本不构成阻力。
    cvol_wall = t.loc[t.call_vol.idxmax()]
    pvol_wall = t.loc[t.put_vol.idxmax()]
    flip = gamma_flip(t)

    print(_c(f"\n{code}  现价 {spot:.2f}   到期 {expiry} ({'0DTE' if dte == 0 else f'{dte}DTE'})"
             f"   {now_et():%m-%d %H:%M ET}", "bold"))
    print(_c("─" * 96, "dim"))
    print(f"  Call Wall  {_c(f'{call_wall.strike:.1f}', 'grn')}  持仓 {_n(call_wall.call_oi)}"
          f"   距现价 {(call_wall.strike/spot-1)*100:+.1f}%")
    print(f"  Put  Wall  {_c(f'{put_wall.strike:.1f}', 'red')}  持仓 {_n(put_wall.put_oi)}"
          f"   距现价 {(put_wall.strike/spot-1)*100:+.1f}%")
    print(f"  {_c('今日成交墙', 'bold')}  CALL {_c(f'{cvol_wall.strike:.1f}', 'grn')}"
          f" 成交 {_n(cvol_wall.call_vol)} (距现价 {(cvol_wall.strike/spot-1)*100:+.1f}%)"
          f"   PUT {_c(f'{pvol_wall.strike:.1f}', 'red')}"
          f" 成交 {_n(pvol_wall.put_vol)} (距现价 {(pvol_wall.strike/spot-1)*100:+.1f}%)")
    if not math.isnan(flip):
        rel = "现价在其上方 → 正 GEX 区，做市商对冲抑制波动" if spot > flip else \
              "现价在其下方 → 负 GEX 区，做市商对冲放大波动"
        print(f"  Gamma Flip {_c(f'{flip:.1f}', 'cyn')}   {rel}")
    print(f"  当日成交   CALL {_n(tot_cv)} / PUT {_n(tot_pv)}"
          f"   C/P = {_c(f'{tot_cv/tot_pv:.2f}', 'grn' if tot_cv > tot_pv else 'red') if tot_pv else 'n/a'}")
    print(f"  存量持仓   CALL {_n(tot_co)} / PUT {_n(tot_po)}"
          f"   C/P = {tot_co/tot_po:.2f}" if tot_po else "")
    print(f"  全链成交/持仓 = {_c(f'{(tot_cv+tot_pv)/(tot_co+tot_po):.2f}x', 'grn') if (tot_co+tot_po) else 'n/a'}"
          f"   （>1 说明当日新建仓已超过存量）")

    print(_c("\n按「成交/持仓比」排序 —— 今天钱在哪儿动", "bold"))
    print(_c(lj("行权价", 9) + rj("距现价", 8) + rj("C成交", 8) + rj("C持仓", 8)
             + rj("C成交/持仓", 11) + rj("P成交", 8) + rj("P持仓", 8)
             + rj("净成交", 9) + rj("C/P成交", 9) + rj("GEX", 10), "dim"))
    print(_c("─" * 96, "dim"))
    act = t[(t.vol >= max(50, t.vol.quantile(0.6)))].copy()
    act["rank_key"] = act[["call_voi", "put_voi"]].max(axis=1).replace(np.inf, 999)
    for _, r in act.sort_values("rank_key", ascending=False).head(12).iterrows():
        atm = "◀" if abs(r.strike - spot) <= (t.strike.diff().median() or 5) / 2 else " "
        voi_c = "grn" if (pd.notna(r.call_voi) and r.call_voi >= 3) else "dim"
        print(lj(f"{r.strike:.1f}{atm}", 9) + rj(f"{(r.strike/spot-1)*100:+.1f}%", 8)
              + rj(_n(r.call_vol), 8) + rj(_n(r.call_oi), 8)
              + rj(_c(_n(r.call_voi, 1) if pd.notna(r.call_voi) else "n/a", voi_c), 11)
              + rj(_n(r.put_vol), 8) + rj(_n(r.put_oi), 8)
              + rj(_c(_n(r.net_vol), "grn" if r.net_vol > 0 else "red"), 9)
              + rj(_n(r.cp_vol_ratio, 2) if pd.notna(r.cp_vol_ratio) else "n/a", 9)
              + rj(_n(r.gex / 1e6, 1) + "M" if pd.notna(r.gex) else "n/a", 10))
    print(_c("─" * 96, "dim"))
    print(_c("◀ = 最接近现价的行权价   净成交 = C成交 − P成交   GEX>0 抑制波动 / <0 放大波动", "dim"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="期权流分析（成交/持仓比 + call/put 失衡）")
    p.add_argument("code", nargs="?", default="US.TSLA")
    p.add_argument("--expiry", help="指定到期日 YYYY-MM-DD，默认取最近（含 0DTE）")
    p.add_argument("--width", type=float, default=12.0, help="只看 spot ±N%%")
    a = p.parse_args(argv)

    with quote_ctx() as q:
        if a.expiry:
            exp = dt.datetime.strptime(a.expiry, "%Y-%m-%d").date()
        else:
            tgt = target_0dte_date(q)
            ex = expiries(q, a.code)
            fut = [x for x in ex if x >= tgt]
            if not fut:
                print(_c(f"{a.code} 没有可用到期日", "red")); return 1
            exp = fut[0]
        d, spot = load_chain(q, a.code, exp)
        t = by_strike(d, spot, a.width)
        dte = (exp - now_et().date()).days
    report(a.code, exp, t, spot, dte)
    return 0


if __name__ == "__main__":
    sys.exit(main())
