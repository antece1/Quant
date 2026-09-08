#!/usr/bin/env python3
"""
APP Q2 2026 post-earnings check: model forecast vs actuals, and what the
Webull position did.

Run it after the print lands (2026-08-05 after close). With no arguments it
pulls the live/after-hours quote from moomoo and reports the position mark.
Pass the reported figures to also score the model:

    ./.venv/bin/python app_earnings_postmortem.py --rev 1.985 --eps 3.93 --ebitda 1.683

Model scenarios come from build_app_model.py (calibrated on Q1 2026 actuals:
adj EBITDA = op profit + D&A + $86M SBC; revenue bridged off the 2025 Q2/Q1
seasonal step, the only clean comp after the Apps divestiture).
"""

from __future__ import annotations
import argparse, math
from statistics import NormalDist

N = NormalDist().cdf
B = 1e9

# --- model output, 2026-08-05 -------------------------------------------------
SCEN = {           # revenue $B, adj EBITDA $B, adj EPS
    "Bear": (1.940, 1.635, 3.76),
    "Base": (1.985, 1.683, 3.93),
    "Bull": (2.035, 1.739, 4.12),
}
STREET_REV, STREET_EPS = 1.940, 3.72
GUIDE = (1.915, 1.945)

def _book(key, fallback):
    """真实持仓从 Options/positions/book.json 读 —— 该文件不进版本控制。
    缺失时回退到 fallback 里的示例数字（编的，不是任何真实仓位）。"""
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "Options" / "positions" / "book.json"
    try:
        return json.loads(p.read_text())[key]
    except (OSError, KeyError, ValueError):
        return fallback

# --- position: 5 legs, entry prices -------------------------------------------
_B = _book("app_earnings_postmortem", {
    "cost": 5000.0,
    "legs": [["C", 420, 1, "S"], ["C", 470, -1, "S"],
             ["P", 370, -2, "S"], ["P", 340, 2, "S"], ["C", 600, 1, "F"]],
})
COST, LEGS = _B["cost"], _B["legs"]
DTE_SEP_AT_ENTRY, DTE_FEB_AT_ENTRY = 44, 197


def bs(kind, S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if kind == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if kind == "C":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def position_value(S, dte_sep, dte_feb, iv_sep, iv_feb):
    return sum(q * bs(k, S, K, (dte_sep if w == "S" else dte_feb) / 365, 0.04,
                      iv_sep if w == "S" else iv_feb) * 100
               for k, K, q, w in LEGS)


def live_quote():
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, s = ctx.get_market_snapshot(["US.APP"])
        if ret != RET_OK:
            raise RuntimeError(s)
        x = s.iloc[0]
        return float(x["last_price"]), float(x["prev_close_price"])
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", type=float, help="reported revenue in $B")
    ap.add_argument("--eps", type=float, help="reported adj EPS")
    ap.add_argument("--ebitda", type=float, help="reported adj EBITDA in $B")
    ap.add_argument("--price", type=float, help="override APP price (else live)")
    ap.add_argument("--iv-sep", type=float, default=0.72)
    ap.add_argument("--iv-feb", type=float, default=0.736)
    a = ap.parse_args()

    if a.price:
        S, prev = a.price, 419.36
    else:
        S, prev = live_quote()
    move = S / prev - 1

    print(f"\n=== APP ${S:.2f}   prev ${prev:.2f}   {move:+.2%} ===")

    if a.rev:
        print(f"\n--- model vs actual ---")
        print(f"{'':8}{'revenue':>10}{'vs model':>10}{'adj EBITDA':>13}{'adj EPS':>10}")
        for k, (rev, eb, eps) in SCEN.items():
            print(f"{k:<8}{rev:>9.3f}B{a.rev/rev-1:>+10.1%}{eb:>12.3f}B{eps:>10.2f}")
        print(f"{'ACTUAL':<8}{a.rev:>9.3f}B{'':>10}"
              f"{(f'{a.ebitda:.3f}B' if a.ebitda else 'n/a'):>13}"
              f"{(f'{a.eps:.2f}' if a.eps else 'n/a'):>10}")
        print(f"\nvs street ${STREET_REV:.3f}B: {a.rev/STREET_REV-1:+.1%}"
              f"   vs guidance high ${GUIDE[1]:.3f}B: {a.rev/GUIDE[1]-1:+.1%}")
        closest = min(SCEN, key=lambda k: abs(SCEN[k][0] - a.rev))
        print(f"closest scenario: {closest}  (model rev ${SCEN[closest][0]:.3f}B)")
        if a.eps:
            print(f"EPS vs street ${STREET_EPS}: {a.eps/STREET_EPS-1:+.1%}")

    print(f"\n--- position (cost ${COST:,.0f}) ---")
    v = position_value(S, DTE_SEP_AT_ENTRY - 1, DTE_FEB_AT_ENTRY - 1, a.iv_sep, a.iv_feb)
    print(f"mark ${v:,.0f}   P&L ${v-COST:+,.0f}  ({v/COST-1:+.1%})"
          f"   [IV assumed Sep {a.iv_sep:.0%} / Feb {a.iv_feb:.0%}]")
    print("\nleg detail:")
    for k, K, q, w in LEGS:
        T = ((DTE_SEP_AT_ENTRY if w == "S" else DTE_FEB_AT_ENTRY) - 1) / 365
        iv = a.iv_sep if w == "S" else a.iv_feb
        px = bs(k, S, K, T, 0.04, iv)
        print(f"  {'Sep' if w=='S' else 'Feb'} ${K} {k}  x{q:+d}   ${px:>7.2f}   ${q*px*100:>+9,.0f}")

    print(f"\n--- vs what we modelled pre-print ---")
    for m, lbl in [(-0.25, ""), (-0.15, ""), (0.0, "flat"), (0.117, "implied move"),
                   (0.20, ""), (0.30, "")]:
        pv = position_value(prev * (1 + m), DTE_SEP_AT_ENTRY - 1, DTE_FEB_AT_ENTRY - 1,
                            a.iv_sep, a.iv_feb) - COST
        mark = "  <-- actual" if abs(m - move) < 0.025 else ""
        print(f"  {m:+6.1%} {lbl:<14} ${pv:>+9,.0f}{mark}")
    print()


if __name__ == "__main__":
    main()
