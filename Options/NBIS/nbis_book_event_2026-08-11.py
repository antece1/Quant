#!/usr/bin/env python3
"""
Reprice the NBIS legs of the Webull book through the Q2 2026 print (2026-08-12).

Differs from nbis_earnings_scenarios.py in the one way that matters now: back in
early August those legs carried 118-126% IV and the crush was the dominant risk.
The surface has since flattened -- Oct/Nov/Jan now sit ~4 vol points above the
fitted base, so crush is nearly gone and these legs are almost pure delta. This
script pulls the CURRENT marks and IVs from moomoo rather than assuming.

Run: ./.venv/bin/python nbis_book_event_2026-08-11.py
"""
from __future__ import annotations
import json
import math
from statistics import NormalDist

import pandas as pd
from futu import OpenQuoteContext, RET_OK

N = NormalDist().cdf
MULT, RFR = 100, 0.04
TODAY = pd.Timestamp("2026-08-11")
BASE_VOL = 0.993          # fitted in nbis_q2_strategy.py
DAYS_FWD = 3              # value on 08/14, the session after the print + 2

def _book(key, fallback):
    """真实持仓从 Options/positions/book.json 读 —— 该文件不进版本控制。
    缺失时回退到 fallback 里的示例数字（编的，不是任何真实仓位）。"""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "positions" / "book.json"
    try:
        return json.loads(p.read_text())[key]
    except (OSError, KeyError, ValueError):
        return fallback

# leg: (expiry, strike, contracts, cost, account)
_B = _book("nbis_book_event", {
    "legs": [["2026-10-16", 250, 1, 2000.00, "taxable"],
             ["2027-01-15", 300, 1, 3000.00, "taxable"]],
    "nbig_sh": 50, "nbig_cost": 1000.00,
})
LEGS = _B["legs"]
NBIG_SH, NBIG_COST = _B["nbig_sh"], _B["nbig_cost"]   # Leverage Shares 2x long NBIS


def bs_call(S, K, T, r, s):
    if T <= 0 or s <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + .5 * s * s) * T) / (s * math.sqrt(T))
    return S * N(d1) - K * math.exp(-r * T) * N(d1 - s * math.sqrt(T))


def code(exp, k):
    return f"US.NBIS{pd.Timestamp(exp):%y%m%d}C{int(round(k*1000))}"


def main():
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, s = ctx.get_market_snapshot(["US.NBIS", "US.NBIG"])[:2]
        S = float(s[s.code == "US.NBIS"].iloc[0]["last_price"])
        nbig = float(s[s.code == "US.NBIG"].iloc[0]["last_price"])
        ret, o = ctx.get_market_snapshot([code(e, k) for e, k, *_ in LEGS])[:2]
    finally:
        ctx.close()
    o = o.set_index("code")

    print(f"\nNBIS ${S:.2f}   NBIG ${nbig:.2f}   valuing {DAYS_FWD}d out (08/14), "
          f"IV -> fitted base {BASE_VOL:.0%}\n")
    print(f"{'leg':<26}{'mark':>9}{'IV':>8}{'crush':>8}{'dte':>5}{'cost':>9}{'now':>9}{'P&L':>10}")
    live = []
    tot_mv = tot_cost = 0.0
    for exp, k, n, cost, acct in LEGS:
        r = o.loc[code(exp, k)]
        mark = (float(r["bid_price"]) + float(r["ask_price"])) / 2
        iv = float(r["option_implied_volatility"]) / 100
        dte = (pd.Timestamp(exp) - TODAY).days
        mv = mark * MULT * n
        tot_mv += mv; tot_cost += cost
        live.append((exp, k, n, cost, acct, mark, iv, dte))
        print(f"{'NBIS '+str(k)+'C '+exp+' '+acct:<26}{mark:>9.2f}{iv:>7.1%}"
              f"{(iv-BASE_VOL)*100:>+7.0f}p{dte:>5}{cost:>9,.0f}{mv:>9,.0f}{mv-cost:>+10,.0f}")
    nb_mv = NBIG_SH * nbig
    tot_mv += nb_mv; tot_cost += NBIG_COST
    print(f"{'NBIG 2x '+str(NBIG_SH)+'sh':<26}{nbig:>9.2f}{'':>8}{'':>8}{'':>5}"
          f"{NBIG_COST:>9,.0f}{nb_mv:>9,.0f}{nb_mv-NBIG_COST:>+10,.0f}")
    print(f"{'NBIS COMPLEX TOTAL':<26}{'':>30}{tot_cost:>9,.0f}{tot_mv:>9,.0f}{tot_mv-tot_cost:>+10,.0f}")

    print(f"\n--- value on 08/14 vs NBIS move (IV crushed to {BASE_VOL:.0%} on every leg) ---")
    moves = [-.30, -.20, -.12, -.06, 0, .06, .12, .20, .30, .45]
    print(f"{'leg':<26}" + "".join(f"{m:>+9.0%}" for m in moves))
    grid_tot = [0.0] * len(moves)
    for exp, k, n, cost, acct, mark, iv, dte in live:
        row = []
        for i, m in enumerate(moves):
            v = bs_call(S * (1 + m), k, max(dte - DAYS_FWD, 0) / 365, RFR, BASE_VOL) * MULT * n
            row.append(v - mark * MULT * n)
            grid_tot[i] += v - mark * MULT * n
        print(f"{'NBIS '+str(k)+'C '+exp:<26}" + "".join(f"{v:>9,.0f}" for v in row))
    row = []
    for i, m in enumerate(moves):
        v = nb_mv * (1 + 2 * m) - nb_mv
        row.append(v); grid_tot[i] += v
    print(f"{'NBIG (2x)':<26}" + "".join(f"{v:>9,.0f}" for v in row))
    print(f"{'TOTAL vs today':<26}" + "".join(f"{v:>9,.0f}" for v in grid_tot))
    print(f"{'  as % of complex':<26}" + "".join(f"{v/tot_mv:>+8.0%} " for v in grid_tot))

    print("\n--- crush-only check: what if NBIS does NOT move at all? ---")
    for exp, k, n, cost, acct, mark, iv, dte in live:
        T = max(dte - DAYS_FWD, 0) / 365
        keep = bs_call(S, k, T, RFR, iv) * MULT * n
        crush = bs_call(S, k, T, RFR, BASE_VOL) * MULT * n
        print(f"  {k}C {exp}: {mark*MULT*n:7,.0f} -> {keep:7,.0f} (theta only) "
              f"-> {crush:7,.0f} (theta+crush)   crush costs {crush-keep:+,.0f}")
    print()


if __name__ == "__main__":
    main()
