#!/usr/bin/env python3
"""
Compare the current long-call book against liquidating it pre-earnings and
selling NBIS put credit spreads instead.

Chain prices below are real moomoo quotes pulled 2026-08-03 (spot 212.58,
NBIS Q2 earnings 2026-08-12, market-implied move into 08-14 = +/-24.8%).
The point of the comparison is that a put credit spread inverts every exposure
the current book has: short vega instead of long, positive theta instead of
negative, capped payoff instead of open-ended.

Run: ./.venv/bin/python put_spread_analysis.py
"""

from __future__ import annotations
import math
from statistics import NormalDist

N = NormalDist().cdf
SPOT = 212.58
IMPLIED_MOVE = 0.248          # priced into 2026-08-14 options
MULT = 100
RFR = 0.04

# strike -> (bid, ask, mid, iv_pct, delta)  — real moomoo quotes 2026-08-03
AUG21 = {
    190: (17.40, 18.45, 17.925, 159.242, -0.308),
    180: (13.50, 14.40, 13.950, 160.029, -0.257),
    175: (11.85, 12.50, 12.175, 160.397, -0.232),
    170: (10.35, 11.00, 10.675, 161.744, -0.208),
    165: ( 8.80,  9.70,  9.250, 162.693, -0.186),
    160: ( 7.50,  8.20,  7.850, 162.746, -0.164),
    155: ( 6.40,  7.20,  6.800, 164.759, -0.145),
    150: ( 5.40,  6.00,  5.700, 165.216, -0.126),
    145: ( 4.55,  5.00,  4.775, 166.247, -0.108),
    140: ( 3.85,  4.30,  4.075, 168.774, -0.094),
}
SEP18 = {
    190: (26.55, 28.15, 27.350, 135.263, -0.315),
    180: (20.95, 23.05, 22.000, 132.820, -0.275),
    175: (18.85, 21.25, 20.050, 133.752, -0.256),
    170: (16.85, 19.05, 17.950, 133.605, -0.236),
    165: (15.00, 17.30, 16.150, 134.258, -0.218),
    160: (13.60, 16.00, 14.800, 136.541, -0.201),
    155: (12.00, 13.80, 12.900, 135.712, -0.182),
    150: (11.20, 12.00, 11.600, 137.338, -0.166),
    145: ( 8.80, 10.85,  9.825, 135.771, -0.148),
    140: ( 8.10, 10.00,  9.050, 139.393, -0.135),
}
DTE = {"2026-08-21": 18, "2026-09-18": 46}

def _book(key, fallback):
    """真实持仓从 Options/positions/book.json 读 —— 该文件不进版本控制。
    缺失时回退到 fallback 里的示例数字（编的，不是任何真实仓位）。"""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "positions" / "book.json"
    try:
        return json.loads(p.read_text())[key]
    except (OSError, KeyError, ValueError):
        return fallback

# Book being liquidated
_B = _book("put_spread_analysis",
           {"book_mv": 10000.00, "book_cost": 10000.00, "cash": 1000.00})
BOOK_MV, BOOK_COST, CASH = _B["book_mv"], _B["book_cost"], _B["cash"]


def bs_put(S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def spreads(chain, dte, label):
    print(f"\n===== NBIS put credit spreads, {label} ({dte} DTE) =====")
    print(f"{'spread':<12}{'credit(mid)':>12}{'credit(real)':>13}{'maxloss':>9}"
          f"{'ROR mid':>9}{'ROR real':>9}{'breakeven':>11}{'BE vs spot':>11}{'vs implied':>12}")
    ks = sorted(chain, reverse=True)
    rows = []
    for i, short_k in enumerate(ks):
        for long_k in ks[i + 1:]:
            width = short_k - long_k
            if width not in (5, 10):
                continue
            s_bid, s_ask, s_mid, s_iv, s_d = chain[short_k]
            l_bid, l_ask, l_mid, l_iv, l_d = chain[long_k]
            cr_mid = s_mid - l_mid                   # both at mid
            cr_real = s_bid - l_ask                  # you sell the bid, buy the ask
            if cr_real <= 0:
                continue
            maxloss = width - cr_real
            be = short_k - cr_real
            be_vs = be / SPOT - 1
            outside = "outside" if be_vs < -IMPLIED_MOVE else "INSIDE"
            rows.append((short_k, long_k, cr_mid, cr_real, maxloss,
                         cr_mid / (width - cr_mid), cr_real / maxloss, be, be_vs, outside))
    for r in sorted(rows, key=lambda x: -x[6])[:12]:
        print(f"{f'{r[0]}/{r[1]}':<12}{r[2]:>12.2f}{r[3]:>13.2f}{r[4]:>9.2f}"
              f"{r[5]:>9.1%}{r[6]:>9.1%}{r[7]:>11.2f}{r[8]:>+11.1%}{r[9]:>12}")
    return rows


def crush_pnl():
    """What a pre-earnings sold spread is worth AFTER the event, spot unchanged."""
    print("\n===== the actual edge: selling 155% IV, then IV crushes =====")
    print("sell Aug-21 spread today, value it 08-13 (5 DTE left) at various IV / spot")
    print(f"{'spread':<12}{'sold for':>9}{'flat@IV90':>11}{'flat@IV70':>11}"
          f"{'-10%@IV90':>11}{'-24.8%@IV90':>13}{'-35%@IV90':>11}")
    for sk, lk in [(190, 180), (170, 160), (160, 150), (150, 140)]:
        s_bid, _, _, s_iv, _ = AUG21[sk]
        _, l_ask, _, l_iv, _ = AUG21[lk]
        credit = s_bid - l_ask
        T = 5 / 365.0
        out = []
        for move, iv in [(0.0, 0.90), (0.0, 0.70), (-0.10, 0.90),
                         (-IMPLIED_MOVE, 0.90), (-0.35, 0.90)]:
            S = SPOT * (1 + move)
            val = bs_put(S, sk, T, RFR, iv) - bs_put(S, lk, T, RFR, iv)
            out.append((credit - val) * MULT)     # P&L per spread
        print(f"{f'{sk}/{lk}':<12}{credit*MULT:>9,.0f}" + "".join(f"{v:>11,.0f}" for v in out[:2])
              + f"{out[2]:>11,.0f}{out[3]:>13,.0f}{out[4]:>11,.0f}")
    print("(P&L per 1 spread, $; conservative fill = sell bid / buy ask)")


def sizing():
    print("\n===== deploying the liquidated book =====")
    liq = BOOK_MV + CASH
    print(f"liquidate everything at Webull marks: ${BOOK_MV:,.0f} + cash ${CASH:,.0f} = ${liq:,.0f}")
    print(f"  (realizes the book's current P&L: {BOOK_MV-BOOK_COST:+,.0f} on ${BOOK_COST:,.0f} cost)")
    print("  NOTE: those marks are mid-ish; long OTM calls at 120%+ IV have wide spreads,")
    print("        so real liquidation lands lower — haircut 3-8% before planning size.\n")
    for sk, lk in [(170, 160), (160, 150), (150, 140)]:
        s_bid, _, _, _, _ = AUG21[sk]
        _, l_ask, _, _, _ = AUG21[lk]
        credit = s_bid - l_ask
        maxloss = (sk - lk) - credit
        for deploy_pct in (0.30, 0.50):
            cap = liq * deploy_pct
            n = int(cap // (maxloss * MULT))
            if n <= 0:
                continue
            print(f"{sk}/{lk} @ {deploy_pct:.0%} of capital (${cap:,.0f}): {n} spreads, "
                  f"credit ${credit*MULT*n:,.0f}, max loss ${maxloss*MULT*n:,.0f}, "
                  f"return-on-capital {credit*MULT*n/cap:+.1%} in 18d")
    print("\nBP note: a defined-risk put spread ties up (width - credit) x 100 per spread")
    print("in BOTH accounts; the Roth needs it fully cash-secured.")


def main():
    print(f"\nspot {SPOT}   earnings 2026-08-12   implied move +/-{IMPLIED_MOVE:.1%} "
          f"(down-move target {SPOT*(1-IMPLIED_MOVE):.2f})")
    spreads(AUG21, DTE["2026-08-21"], "2026-08-21 — spans earnings, IV 159-169%")
    spreads(SEP18, DTE["2026-09-18"], "2026-09-18 — spans earnings, IV 133-139%")
    crush_pnl()
    sizing()
    print()


if __name__ == "__main__":
    main()
