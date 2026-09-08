"""
Put 视角的期权扫描（默认 Mag7）。

做 put 和做 call 关注的东西不一样：
  IV/RV      put 的隐波相对这只票真实波动贵不贵。买 put 时 <1 才划算。
  Skew       ATM put IV − ATM call IV。正常情况下 put 更贵（下跌保护需求），
             skew 越大说明市场已经在抢保护，这时买 put 是在追高保险费。
  P/C 成交比  全链 put 成交 / call 成交。>1 说明今天资金偏空。
  put 成交/持仓  新建的空头保护仓有多少，是"现在正在发生"的信号。

排序默认按已实现波动率（RV）—— 波动越大，同样 delta 的 put 越有兑现空间。

用法:
    python -m premkt.putscan                    # Mag7
    python -m premkt.putscan US.AAPL US.TSLA
    python -m premkt.putscan --sort iv_rv       # 改按 put 便宜程度排
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import numpy as np
import pandas as pd

from .data import daily_klines, now_et, quote_ctx, snapshots
from .dual import _vol_metrics
from .fmt import c as _c, lj, rj
from .optflow import load_chain
from .options import expiries, target_0dte_date

MAG7 = ["US.AAPL", "US.MSFT", "US.GOOGL", "US.AMZN", "US.NVDA", "US.META", "US.TSLA"]


def _f(v, d=np.nan):
    try:
        x = float(v)
        return d if math.isnan(x) else x
    except (TypeError, ValueError):
        return d


def _atm_row(df: pd.DataFrame, spot: float, kind: str):
    sub = df[df.option_type == kind]
    if sub.empty:
        return None
    return sub.iloc[(sub.strike - spot).abs().argsort().iloc[0]]


def analyse(q, code: str, today: dt.date, kl: dict) -> dict:
    exps = expiries(q, code)
    tgt = target_0dte_date(q)
    fut = [x for x in exps if x >= tgt]
    if not fut:
        return dict(code=code, ok=False)
    exp = fut[0]
    d, spot = load_chain(q, code, exp)

    snap = snapshots(q, [code], quiet=True).iloc[0]
    prev = _f(snap["prev_close_price"])
    chg = (spot / prev - 1) * 100 if prev > 0 else np.nan
    vm = _vol_metrics(kl.get(code), spot, today)

    put = _atm_row(d, spot, "PUT")
    call = _atm_row(d, spot, "CALL")
    p_iv = _f(put["iv"]) if put is not None else np.nan
    c_iv = _f(call["iv"]) if call is not None else np.nan
    p_bid, p_ask = (_f(put["bid"]), _f(put["ask"])) if put is not None else (np.nan, np.nan)
    p_mid = (p_bid + p_ask) / 2 if not math.isnan(p_bid + p_ask) else np.nan

    puts, calls = d[d.option_type == "PUT"], d[d.option_type == "CALL"]
    pv, cv = puts.vol.sum(), calls.vol.sum()
    poi = puts.oi.sum()

    return dict(
        code=code, ok=True, spot=spot, chg=chg,
        amplitude=_f(snap["amplitude"]), atr_pct=vm["atr_pct"], rvol=vm["rvol"],
        exp=exp, dte=(exp - today).days,
        atm_strike=_f(put["strike"]) if put is not None else np.nan,
        put_iv=p_iv, call_iv=c_iv,
        # 列名不能叫 skew —— 会撞 pandas 的 Series.skew() 方法，
        # 属性式取值会静默拿到方法对象（同类坑：last / squeeze）
        put_skew=p_iv - c_iv if not (math.isnan(p_iv) or math.isnan(c_iv)) else np.nan,
        iv_rv=p_iv / vm["rvol"] if (not math.isnan(p_iv) and vm["rvol"] and vm["rvol"] > 0) else np.nan,
        put_vol=_f(put["vol"]) if put is not None else np.nan,
        put_oi=_f(put["oi"]) if put is not None else np.nan,
        put_voi=(_f(put["vol"]) / _f(put["oi"])) if put is not None and _f(put["oi"]) > 0 else np.nan,
        put_mid=p_mid,
        put_cost_pct=p_mid / spot * 100 if not math.isnan(p_mid) and spot else np.nan,
        put_spread=(p_ask - p_bid) / p_mid * 100 if not math.isnan(p_mid) and p_mid > 0 else np.nan,
        pc_vol=pv / cv if cv > 0 else np.nan,
        chain_put_oi=poi,
    )


def _n(v, nd=1, suf=""):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f}M{suf}"
    if abs(v) >= 1e3:
        return f"{v/1e3:.1f}K{suf}"
    return f"{v:.{nd}f}{suf}"


def report(rows: list[dict], sort_key: str) -> None:
    df = pd.DataFrame([r for r in rows if r.get("ok")])
    if df.empty:
        print(_c("没有可用数据。", "yel")); return
    asc = sort_key == "iv_rv"          # 买 put 时 IV/RV 越小越好
    df = df.sort_values(sort_key, ascending=asc, na_position="last").reset_index(drop=True)

    print(_c(f"\nMag7 · Put 视角   {now_et():%Y-%m-%d %H:%M ET}   "
             f"按 {'IV/RV(越低越便宜)' if asc else 'RV(已实现波动)'} 排序", "bold"))
    print(_c(lj("代码", 10) + rj("现价", 10) + rj("今日", 8) + rj("振幅", 7)
             + rj("ATR%", 7) + rj("RV", 6) + rj("到期", 8) + rj("DTE", 5)
             + rj("ATM", 9) + rj("putIV", 7) + rj("IV/RV", 7) + rj("skew", 7)
             + rj("权利金", 9) + rj("占股价", 8) + rj("点差", 7)
             + rj("P成交", 8) + rj("成交/持仓", 10) + rj("链P/C", 8), "bold"))
    print(_c("─" * 148, "dim"))
    for _, r in df.iterrows():
        ivrv_c = "grn" if (pd.notna(r.iv_rv) and r.iv_rv < 1.0) else (
            "yel" if (pd.notna(r.iv_rv) and r.iv_rv < 1.4) else "red")
        skew_c = "red" if (pd.notna(r.put_skew) and r.put_skew > 8) else "dim"
        pc_c = "red" if (pd.notna(r.pc_vol) and r.pc_vol > 1.0) else "dim"
        print(lj(r.code, 10) + rj(f"{r.spot:.2f}", 10)
              + rj(_c(f"{r.chg:+.1f}%", "grn" if r.chg > 0 else "red"), 8)
              + rj(f"{r.amplitude:.1f}%", 7)
              + rj("n/a" if pd.isna(r.atr_pct) else f"{r.atr_pct:.1f}%", 7)
              + rj("n/a" if pd.isna(r.rvol) else f"{r.rvol:.0f}", 6)
              + rj(str(r.exp)[5:], 8) + rj(str(int(r.dte)), 5)
              + rj(f"{r.atm_strike:.0f}", 9)
              + rj("n/a" if pd.isna(r.put_iv) else f"{r.put_iv:.0f}", 7)
              + rj(_c("n/a" if pd.isna(r.iv_rv) else f"{r.iv_rv:.2f}", ivrv_c), 7)
              + rj(_c("n/a" if pd.isna(r.put_skew) else f"{r.put_skew:+.0f}", skew_c), 7)
              + rj("n/a" if pd.isna(r.put_mid) else f"{r.put_mid:.2f}", 9)
              + rj("n/a" if pd.isna(r.put_cost_pct) else f"{r.put_cost_pct:.2f}%", 8)
              + rj("n/a" if pd.isna(r.put_spread) else f"{r.put_spread:.0f}%", 7)
              + rj(_n(r.put_vol, 0), 8)
              + rj("n/a" if pd.isna(r.put_voi) else f"{r.put_voi:.1f}x", 10)
              + rj(_c("n/a" if pd.isna(r.pc_vol) else f"{r.pc_vol:.2f}", pc_c), 8))
    print(_c("─" * 148, "dim"))
    print(_c("IV/RV<1 = put 比这只票真实波动便宜   skew = ATM put IV − call IV，越大说明保护越抢手", "dim"))
    print(_c("权利金/占股价 = 买 1 张 ATM put 的中间价与其占正股价格的比例（即到期需要跌多少才回本）", "dim"))
    print(_c("链P/C = 全链 put 成交 / call 成交，>1 说明当日资金偏空", "dim"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Put 视角期权扫描（默认 Mag7）")
    p.add_argument("codes", nargs="*", default=None)
    p.add_argument("--sort", choices=["rvol", "iv_rv", "atr_pct", "put_skew"], default="rvol")
    a = p.parse_args(argv)
    codes = a.codes or MAG7
    today = now_et().date()
    rows = []
    with quote_ctx() as q:
        kl = daily_klines(q, codes)
        for c in codes:
            try:
                rows.append(analyse(q, c, today, kl))
            except Exception as exc:
                print(_c(f"  {c} 失败: {exc}", "yel"))
    report(rows, a.sort)
    return 0


if __name__ == "__main__":
    sys.exit(main())
