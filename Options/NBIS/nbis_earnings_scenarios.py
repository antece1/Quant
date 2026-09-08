#!/usr/bin/env python3
"""
Reprice the whole Webull book across the NBIS Q2 earnings event (2026-08-12).

Why this exists: the book's NBIS legs carry 118-126% IV and the front weeklies
sit at ~176%. After a binary event that vol collapses, so a "right direction"
call can still lose. Freezing IV (what webull_portfolio_analysis.py's grid does)
understates that. Here spot move and IV crush move together.

Valuation date = 2026-08-13, the session after earnings (10 days from the
2026-08-03 snapshot). Non-NBIS names keep spot flat and just bleed 10 days of
theta -- they have their own catalysts, not this one.

Run: ./.venv/bin/python nbis_earnings_scenarios.py
"""

from __future__ import annotations
import math
from statistics import NormalDist

N = NormalDist().cdf
MULT = 100
SNAP_DATE = "2026-08-03"
EARN_DATE = "2026-08-12"
DAYS_FWD = 10                      # snapshot -> day after earnings
RFR = 0.04
SPOT = {"NBIS": 212.58, "BE": 218.32, "CRDO": 218.35, "ASX": 36.68}

def _book(key, fallback):
    """真实持仓从 Options/positions/book.json 读 —— 该文件不进版本控制。
    缺失时回退到 fallback 里的示例数字（编的，不是任何真实仓位）。"""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "positions" / "book.json"
    try:
        return json.loads(p.read_text())[key]
    except (OSError, KeyError, ValueError):
        return fallback

# leg: (ticker, strike, dte_at_snapshot, iv_pct, contracts, mark, cost, account)
# leveraged ETF: (ticker, economic underlying, leverage, shares, mark, cost)
_B = _book("nbis_earnings_scenarios", {
    "options": [["NBIS", 250, 60, 100.0, 1, 20.000, 2000.00, "taxable"],
                ["BE",   300, 120, 110.0, 1, 30.000, 3000.00, "taxable"]],
    "etfs":    [["NBIG", "NBIS", 2.0, 50, 20.00, 1000.00]],
})
OPTIONS, ETFS = _B["options"], _B["etfs"]
BOOK_MV = sum(o[5] * MULT * o[4] for o in OPTIONS) + sum(e[3] * e[4] for e in ETFS)
BOOK_COST = sum(o[6] for o in OPTIONS) + sum(e[5] for e in ETFS)


def bs_call(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return S * N(d1) - K * math.exp(-r * T) * N(d1 - sig * math.sqrt(T))


def book_value(nbis_move, iv_mult_nbis, iv_mult_other=1.0):
    """Value the book DAYS_FWD later given an NBIS move and an IV crush factor."""
    tot = 0.0
    for tk, K, dte, iv, n, mark, cost, _ in OPTIONS:
        if tk == "NBIS":
            S = SPOT[tk] * (1 + nbis_move)
            sig = iv / 100.0 * iv_mult_nbis
        else:
            S = SPOT[tk]
            sig = iv / 100.0 * iv_mult_other
        T = max(dte - DAYS_FWD, 0) / 365.0
        tot += bs_call(S, K, T, RFR, sig) * MULT * n
    for tk, econ, lev, sh, mark, cost in ETFS:
        move = nbis_move if econ == "NBIS" else 0.0
        tot += sh * mark * (1 + lev * move)
    return tot


def main():
    print(f"\n=== NBIS Q2 earnings ({EARN_DATE}) — book repriced {DAYS_FWD}d out ===")
    print(f"snapshot {SNAP_DATE}: book MV ${BOOK_MV:,.0f}, cost ${BOOK_COST:,.0f}")
    print(f"market-implied move into 08-14: +/-24.8%  (pre-earnings weekly +/-14.0%)\n")

    moves = [-0.35, -0.25, -0.15, -0.05, 0.0, 0.05, 0.15, 0.25, 0.35]
    for label, ivm in [("no IV crush (IV stays 118-126%)", 1.00),
                       ("moderate crush (IV x0.75 -> ~90%)", 0.75),
                       ("hard crush (IV x0.60 -> ~72%)", 0.60)]:
        print(f"--- {label} ---")
        head = "NBIS move " + "".join(f"{m:>+9.0%}" for m in moves)
        print(head)
        vals = [book_value(m, ivm) for m in moves]
        print("book value " + "".join(f"{v:>9,.0f}" for v in vals))
        print("vs today   " + "".join(f"{v/BOOK_MV-1:>+9.0%}" for v in vals))
        print()

    print("--- NBIS legs only, at the implied +/-24.8% move ---")
    print(f"{'leg':<22}{'mark':>8}{'-24.8% crush':>14}{'flat crush':>12}{'+24.8% crush':>14}{'+24.8% no crush':>17}")
    for tk, K, dte, iv, n, mark, cost, acct in OPTIONS:
        if tk != "NBIS":
            continue
        T = max(dte - DAYS_FWD, 0) / 365.0
        row = []
        for mv, ivm in [(-0.248, 0.75), (0.0, 0.75), (0.248, 0.75), (0.248, 1.0)]:
            S = SPOT[tk] * (1 + mv)
            row.append(bs_call(S, K, T, RFR, iv / 100.0 * ivm) * MULT * n)
        print(f"{tk} {K}C {acct:<12}{mark*MULT*n:>8,.0f}" + "".join(f"{v:>14,.0f}" for v in row[:3])
              + f"{row[3]:>17,.0f}")

    print("\n--- breakeven check: what NBIS spot each leg needs at expiry ---")
    for tk, K, dte, iv, n, mark, cost, acct in OPTIONS:
        entry = cost / (MULT * n)
        be = K + entry
        print(f"{tk} {K}C {acct:<10} entry {entry:>7.2f}  breakeven {be:>7.2f}  "
              f"spot {SPOT[tk]:>7.2f}  needs {be/SPOT[tk]-1:>+7.1%}")
    print()


if __name__ == "__main__":
    main()
