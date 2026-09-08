#!/usr/bin/env python3
"""
SMH put-hedge backtest — Black-Scholes PROXY version.

Tests the rule: "when SMH implied-vol is high (IV rank > threshold),
buy an ~8% OTM put, ~30 DTE, as a hedge for a long AI/semis book."

This is a PROXY backtest: it does NOT use a real historical option chain.
Instead it prices each synthetic put with Black-Scholes, using an IV input
you supply. Two IV modes are provided:
  - 'hv'   : IV proxy = realized vol * a multiplier (free, rough)
  - 'csv'  : IV from a CSV you provide (e.g. VXN/SOXX-IV) -> much better
Replace the proxy with a real EOD option chain (ORATS / OptionMetrics /
CBOE DataShop / Polygon) when you want production-grade numbers.

The hedge is evaluated as an OVERLAY on a long book, because standalone a
put hedge is expected to lose money (it is insurance). What matters is
drawdown / tail reduction on the combined equity curve.

Deps: numpy, pandas, and (optionally) yfinance for the price download.
    pip install numpy pandas yfinance
Run:  python3 smh_put_hedge_backtest.py
"""

from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
import pandas as pd

# ----------------------------- CONFIG ---------------------------------------
TICKER          = "SMH"
START           = "2019-01-01"
END             = None            # None = today
IV_MODE         = "hv"            # "hv" or "csv"
IV_CSV_PATH     = None            # if IV_MODE=="csv": CSV with columns date,iv (iv as decimal, e.g. 0.32)
HV_WINDOW       = 21              # trading days for realized-vol
IV_HV_MULT      = 1.15           # IV proxy = HV * this (IV usually > HV); tune it
IVRANK_WINDOW   = 252            # lookback for IV-rank percentile
IVRANK_THRESH   = 90             # enter when IV rank > this
OTM_PCT         = 0.08           # buy put this far OTM (0.08 = 8%)
DTE             = 30             # days to expiry at entry (calendar)
RFR             = 0.04           # risk-free rate (annual)
CONTRACTS       = 4              # puts per signal (or size off beta-weighted delta)
MULT            = 100            # option contract multiplier
SLIPPAGE_PCT    = 0.05           # haircut on entry premium for spread/slippage (options are wide!)
def _book(key, fallback):
    """真实持仓从 Options/positions/book.json 读 —— 该文件不进版本控制。
    缺失时回退到 fallback 里的示例数字（编的，不是任何真实仓位）。"""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "positions" / "book.json"
    try:
        return json.loads(p.read_text())[key]
    except (OSError, KeyError, ValueError):
        return fallback

# Portfolio overlay: approximate the book as a levered long-SMH exposure.
BOOK_NOTIONAL   = _book("smh_put_hedge", {"book_notional": 100_000})["book_notional"]
# ----------------------------------------------------------------------------


def bs_put(S, K, T, r, sigma):
    """Black-Scholes European put price."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    from statistics import NormalDist
    N = NormalDist().cdf
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def load_prices() -> pd.DataFrame:
    try:
        import yfinance as yf
        df = yf.download(TICKER, start=START, end=END, auto_adjust=True, progress=False)
        px = df["Close"].rename("close").to_frame()
        px.index = pd.to_datetime(px.index)
        return px
    except Exception as e:
        raise SystemExit(
            f"Could not download {TICKER} via yfinance ({e}). "
            f"Install yfinance or load a CSV of date,close into load_prices()."
        )


def build_iv(px: pd.DataFrame) -> pd.Series:
    """Return an implied-vol series (decimal annualized) indexed like px."""
    ret = np.log(px["close"] / px["close"].shift(1))
    hv = ret.rolling(HV_WINDOW).std() * math.sqrt(252)
    if IV_MODE == "hv":
        return (hv * IV_HV_MULT).rename("iv")
    if IV_MODE == "csv":
        if not IV_CSV_PATH:
            raise SystemExit("IV_MODE='csv' but IV_CSV_PATH is None.")
        iv = pd.read_csv(IV_CSV_PATH, parse_dates=["date"]).set_index("date")["iv"]
        return iv.reindex(px.index).ffill().rename("iv")
    raise SystemExit(f"Unknown IV_MODE {IV_MODE!r}")


def iv_rank(iv: pd.Series) -> pd.Series:
    """Rolling min-max IV rank within [0,100]. Uses only past data (no look-ahead)."""
    lo = iv.rolling(IVRANK_WINDOW).min()
    hi = iv.rolling(IVRANK_WINDOW).max()
    return ((iv - lo) / (hi - lo) * 100).rename("ivrank")


@dataclass
class Trade:
    entry: pd.Timestamp
    expiry: pd.Timestamp
    S0: float
    K: float
    premium: float          # per share, after slippage
    payoff: float = 0.0     # per share at expiry
    pnl: float = 0.0        # total $ incl. multiplier & contracts


def run():
    px = load_prices()
    iv = build_iv(px)
    ivr = iv_rank(iv)
    data = px.join(iv).join(ivr).dropna()

    trades: list[Trade] = []
    open_until = pd.Timestamp.min
    for dt, row in data.iterrows():
        if dt <= open_until:
            continue  # already hold a hedge
        if row["ivrank"] > IVRANK_THRESH:
            S0 = float(row["close"])
            K = round(S0 * (1 - OTM_PCT), 0)
            T = DTE / 365.0
            prem = bs_put(S0, K, T, RFR, float(row["iv"])) * (1 + SLIPPAGE_PCT)
            expiry = dt + pd.Timedelta(days=DTE)
            trades.append(Trade(dt, expiry, S0, K, prem))
            open_until = expiry

    # settle each trade at expiry (nearest available close)
    for t in trades:
        idx = data.index[data.index >= t.expiry]
        S_exp = float(data.loc[idx[0], "close"]) if len(idx) else float(data["close"].iloc[-1])
        t.payoff = max(t.K - S_exp, 0.0)
        t.pnl = (t.payoff - t.premium) * MULT * CONTRACTS

    # ---- strategy-layer stats ----
    pnls = np.array([t.pnl for t in trades]) if trades else np.array([])
    n = len(trades)
    total = pnls.sum() if n else 0.0
    wins = (pnls > 0).sum() if n else 0
    print(f"\n=== SMH put-hedge backtest ({IV_MODE} IV proxy) ===")
    print(f"period            : {data.index[0].date()} -> {data.index[-1].date()}")
    print(f"signals (trades)  : {n}   (IV rank > {IVRANK_THRESH})")
    if n:
        print(f"hit rate          : {wins}/{n} = {wins/n:.0%}")
        print(f"avg premium paid  : ${np.mean([t.premium for t in trades])*MULT*CONTRACTS:,.0f} / trade")
        print(f"total hedge P&L   : ${total:,.0f}   (negative = net insurance cost)")
        print(f"best trade        : ${pnls.max():,.0f}   worst: ${pnls.min():,.0f}")

    # ---- portfolio-overlay stats (the real test) ----
    # naked book = BOOK_NOTIONAL of long SMH; hedged = book + hedge daily MtM (approx: settle at expiry)
    daily_ret = data["close"].pct_change().fillna(0)
    book_equity = (1 + daily_ret).cumprod() * BOOK_NOTIONAL
    hedge_cf = pd.Series(0.0, index=data.index)
    for t in trades:
        # cost at entry, payoff at expiry (cash-flow approximation; ignores intra-life MtM)
        hedge_cf.loc[t.entry] += -t.premium * MULT * CONTRACTS
        idx = data.index[data.index >= t.expiry]
        settle = idx[0] if len(idx) else data.index[-1]
        hedge_cf.loc[settle] += t.payoff * MULT * CONTRACTS
    hedged_equity = book_equity + hedge_cf.cumsum()

    def max_dd(eq):
        return ((eq - eq.cummax()) / eq.cummax()).min()

    def cvar(eq, q=0.05):
        r = eq.pct_change().dropna()
        var = r.quantile(q)
        return r[r <= var].mean()

    print("\n--- overlay on long book ---")
    print(f"naked  max drawdown: {max_dd(book_equity):.1%}   CVaR5%: {cvar(book_equity):.2%}")
    print(f"hedged max drawdown: {max_dd(hedged_equity):.1%}   CVaR5%: {cvar(hedged_equity):.2%}")
    print(f"drag from hedge    : ${(hedged_equity.iloc[-1]-book_equity.iloc[-1]):,.0f} on final equity")
    print("\nNOTE: BS-proxy pricing + expiry-only settlement. For real numbers,")
    print("swap build_iv()/bs_put() for a historical EOD option chain.\n")

    return trades, book_equity, hedged_equity


if __name__ == "__main__":
    run()
