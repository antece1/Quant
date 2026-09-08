#!/usr/bin/env python3
"""
Portfolio backtest/risk read for the recorded Webull accounts.

Entry prices are EXACT (your Webull avg cost). Current marks are EXACT (Webull
last). What this adds on top: live underlying spot + option greeks/IV pulled from
YOUR moomoo OpenAPI (OpenD on 127.0.0.1:11111), then per-leg breakeven, moneyness,
intrinsic/extrinsic split, theta bleed, delta-equivalent notional, and
concentration by underlying.

Usage: python3 webull_portfolio_analysis.py [positions.json]
"""

from __future__ import annotations
import json
import sys
from datetime import date
import pandas as pd

import pathlib as _pl, sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
from optlib import find, chain_for, data_dir   # noqa: E402
from moomoo_option_price import futu_option_code



TODAY = pd.Timestamp(date.today())
DEFAULT_FILE = find("webull_positions_2026-08-03.json")
MULT = 100

# Leveraged single-stock ETFs -> (economic underlying, leverage factor).
# NBIG is a 2x NBIS ETF and CRDU a 2x CRDO ETF, so they are NOT independent
# names: they stack on top of the NBIS / CRDO option exposure.
LEVERAGED_ETF = {"NBIG": ("NBIS", 2.0), "CRDU": ("CRDO", 2.0), "BEG": ("BE", 2.0)}


def snapshot(codes, host="127.0.0.1", port=11111):
    """code -> snapshot dict, using moomoo's market snapshot (stocks + options)."""
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=host, port=port)
    out = {}
    try:
        for i in range(0, len(codes), 200):
            ret, data = ctx.get_market_snapshot(codes[i:i + 200])
            if ret != RET_OK:
                print(f"[warn] snapshot failed: {data}")
                continue
            for _, r in data.iterrows():
                out[r["code"]] = r.to_dict()
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return out


def f(v, default=float("nan")):
    try:
        x = float(v)
        return default if pd.isna(x) else x
    except Exception:
        return default


def bs_call(S, K, T, r, sig):
    import math
    from statistics import NormalDist
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0)
    N = NormalDist().cdf
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return S * N(d1) - K * math.exp(-r * T) * N(d1 - sig * math.sqrt(T))


def scenarios(df, moves=(-0.30, -0.20, -0.10, 0.0, 0.10, 0.20, 0.30, 0.50),
              horizons=(0, 30, 60), rfr=0.04):
    """Reprice the whole book with BS at frozen IV for a uniform % move in every
    underlying, at several holding horizons (calendar days from today)."""
    print("\n---------------- scenario grid (uniform move in all underlyings, IV frozen) ----------------")
    base_mv = df["mv"].sum()
    base_cost = df["cost"].sum()
    out = []
    for h in horizons:
        row = {"horizon_d": h}
        for m in moves:
            tot = 0.0
            for _, r in df.iterrows():
                if r["kind"] == "stock":
                    # 2x daily ETF: approximate the path-independent case as lev x move
                    tot += r["mv"] * (1 + r["lev"] * m)
                else:
                    S = r["spot"] * (1 + m)
                    T = max(r["dte"] - h, 0) / 365.0
                    tot += bs_call(S, r["strike"], T, rfr, r["iv"] / 100.0) * MULT * r["qty"]
            row[f"{m:+.0%}"] = tot
        out.append(row)
    grid = pd.DataFrame(out).set_index("horizon_d")
    print(grid.to_string(float_format=lambda x: f"{x:,.0f}"))
    print(f"(today's mark ${base_mv:,.0f}; cost basis ${base_cost:,.0f} — "
          f"cells above cost are a full-book recovery)")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    book = json.load(open(path))

    legs = []
    for acct in book["accounts"]:
        for p in acct["positions"]:
            p = dict(p)
            p["account"] = acct["label"]
            p["code"] = (f"US.{p['ticker']}" if p["kind"] == "stock"
                         else futu_option_code(p["ticker"], p["expiry"], p["strike"], p["type"]))
            legs.append(p)

    underlyings = sorted({f"US.{p['ticker']}" for p in legs})
    snaps = snapshot(sorted({l["code"] for l in legs}) + underlyings)

    rows = []
    for l in legs:
        u = snaps.get(f"US.{l['ticker']}", {})
        spot = f(u.get("last_price"))
        prev = f(u.get("prev_close_price"))
        s = snaps.get(l["code"], {})
        econ, lev = LEVERAGED_ETF.get(l["ticker"], (l["ticker"], 1.0))
        row = {
            "account": l["account"].split("—")[-1].strip(),
            "leg": (l["ticker"] if l["kind"] == "stock"
                    else f"{l['ticker']} {l['strike']}{l['type'][0].upper()} {l['expiry'][2:]}"),
            "kind": l["kind"], "ticker": l["ticker"],
            "econ": econ, "lev": lev,
            "u_today_%": spot / prev - 1 if prev == prev and prev else float("nan"),
            "qty": l.get("qty", l.get("contracts")),
            "cost": l["cost"], "mv": l["mkt_value"],
            "pnl": l["open_pnl"], "pnl_pct": l["open_pnl_pct"],
            "entry": l["avg_price"], "mark": l["last"], "spot": spot,
        }
        if l["kind"] == "option":
            K, n = l["strike"], l["contracts"]
            dte = (pd.Timestamp(l["expiry"]) - TODAY).days
            intrinsic = max(spot - K, 0.0) if l["type"] == "call" else max(K - spot, 0.0)
            extrinsic = l["last"] - intrinsic
            be = K + l["avg_price"]
            delta = f(s.get("option_delta"))
            theta = f(s.get("option_theta"))
            row.update({
                "dte": dte, "strike": K,
                "moneyness": spot / K if spot == spot else float("nan"),
                "breakeven": be,
                "to_be_pct": be / spot - 1 if spot == spot else float("nan"),
                "to_strike_pct": K / spot - 1 if spot == spot else float("nan"),
                "intrinsic": intrinsic * MULT * n,
                "extrinsic": extrinsic * MULT * n,
                "iv": f(s.get("option_implied_volatility")),
                "delta": delta,
                "theta_day": theta * MULT * n if theta == theta else float("nan"),
                "delta_notional": delta * MULT * n * spot if delta == delta and spot == spot else float("nan"),
                "opt_last_moomoo": f(s.get("last_price")),
                "oi": f(s.get("option_open_interest")),
            })
        else:
            # 2x ETF: $1 of ETF gives $2 of daily exposure to the economic underlying
            row.update({"delta_notional": row["qty"] * spot * lev if spot == spot else float("nan")})
        rows.append(row)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250, "display.max_columns", 50)

    print(f"\n================ WEBULL BOOK — analysis run {TODAY.date()} ================")
    for acct, g in df.groupby("account", sort=False):
        cost, mv = g["cost"].sum(), g["mv"].sum()
        print(f"\n### {acct}   cost ${cost:,.0f} -> mv ${mv:,.0f}   "
              f"P&L ${mv-cost:,.0f} ({mv/cost-1:+.1%})")
        cols = ["leg", "qty", "entry", "mark", "spot", "cost", "mv", "pnl", "pnl_pct",
                "dte", "moneyness", "breakeven", "to_be_pct", "iv", "delta",
                "theta_day", "intrinsic", "extrinsic", "delta_notional"]
        cols = [c for c in cols if c in g.columns]
        print(g[cols].to_string(index=False, float_format=lambda x: f"{x:,.3f}"))

    opts = df[df["kind"] == "option"]
    print("\n---------------- portfolio risk ----------------")
    tot_cost, tot_mv = df["cost"].sum(), df["mv"].sum()
    print(f"total cost ${tot_cost:,.0f} -> mv ${tot_mv:,.0f}   P&L ${tot_mv-tot_cost:,.0f} ({tot_mv/tot_cost-1:+.1%})")
    if not opts.empty:
        print(f"option MV ${opts['mv'].sum():,.0f} = {opts['mv'].sum()/tot_mv:.0%} of book   "
              f"intrinsic ${opts['intrinsic'].sum():,.0f} / extrinsic ${opts['extrinsic'].sum():,.0f}")
        print(f"theta bleed ${opts['theta_day'].sum():,.0f}/day  "
              f"(= {abs(opts['theta_day'].sum())/tot_mv:.2%} of book per day, "
              f"${opts['theta_day'].sum()*30:,.0f}/month)")
        print(f"delta-equivalent notional ${df['delta_notional'].sum():,.0f}  "
              f"({df['delta_notional'].sum()/tot_mv:.1f}x book leverage)")

    for key, title in (("ticker", "by ticker"), ("econ", "by ECONOMIC underlying (2x ETFs folded in)")):
        print(f"\n{title}:")
        by = df.groupby(key).agg(cost=("cost", "sum"), mv=("mv", "sum"),
                                 pnl=("pnl", "sum"), delta_notional=("delta_notional", "sum"),
                                 u_today=("u_today_%", "max"))
        by["mv_%book"] = by["mv"] / tot_mv
        by["dn_%book"] = by["delta_notional"] / tot_mv
        by["ret"] = by["mv"] / by["cost"] - 1
        print(by.sort_values("mv", ascending=False).to_string(
            float_format=lambda x: f"{x:,.2f}"))
    scenarios(df)

    print("\nunderlying moves today (moomoo regular-session close vs prev close):")
    print(df.drop_duplicates("ticker").set_index("ticker")[["spot", "u_today_%"]]
          .to_string(float_format=lambda x: f"{x:,.3f}"))
    print()


if __name__ == "__main__":
    main()
