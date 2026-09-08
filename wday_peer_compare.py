#!/usr/bin/env python3
"""Why WDAY lags MSFT / TEAM / CRM / CRWD / NOW.  Data 2026-08-27 intraday."""
B = 1e9
# ticker: price, day%, mktcap, ttm_rev, rev_growth, ttm_gaap_eps, fwd_pe, trail_pe, pt, lo, hi
D = {
 "CRM":  (249.33, +21.26, 204.20*B, 43.94*B, 0.112, 10.79, 17.09, None, 257.18, 146.32, 269.11),
 "CRWD": (222.55, +17.64, 226.61*B,  5.40*B, 0.243,  0.04, 158.27, None, 219.64,  85.68, 227.50),
 "NOW":  (137.05,  +8.94, 141.69*B, 14.73*B, 0.222,  1.60, 27.76, None, 142.23,  81.24, 194.73),
 "TEAM": (183.46,  +8.90,  46.44*B,  6.57*B, 0.260, -0.21, 31.07, None, 188.77,  56.01, 184.40),
 "MSFT": (504.50,  +1.64,   3.75e12, 331.84*B, 0.178, 17.95, 25.13, 27.65, 569.45, 349.20, 553.72),
 "WDAY": (198.09,  +3.85,  48.92*B,  9.85*B, 0.133,  3.21, 17.16, 59.40, 188.21, 110.36, 249.85),
}
ORDER = ["CRM", "CRWD", "NOW", "TEAM", "MSFT", "WDAY"]

print("=" * 108)
print("TODAY (2026-08-27): the enterprise-software complex re-rated. WDAY did not.")
print("=" * 108)
print(f"{'':6s}{'price':>9s}{'today':>9s}{'mkt cap':>10s}{'TTM rev':>10s}{'growth':>9s}"
      f"{'fwd P/E':>9s}{'GAAP P/E':>10s}{'vs 52w hi':>11s}{'vs PT':>9s}")
print("-" * 108)
for t in ORDER:
    px, dy, mc, rev, g, eps, fpe, tpe, pt, lo, hi = D[t]
    tpe = tpe if tpe else (px/eps if eps > 0.5 else float('nan'))
    mcs = f"{mc/1e12:.2f}T" if mc > 1e12 else f"{mc/B:.0f}B"
    tpes = f"{tpe:>9.1f}" if tpe == tpe else "      n/m"
    mark = "  <-- " if t == "WDAY" else ""
    print(f"{t:6s}{px:>9.2f}{dy:>+8.2f}%{mcs:>10s}{rev/B:>9.1f}B{g:>8.1%}"
          f"{fpe:>9.1f}{tpes:>10s}{px/hi-1:>+10.1%}{pt/px-1:>+8.1%}{mark}")

print("\n" + "=" * 108)
print("THE KEY FINDING: WDAY AND CRM TRADE AT THE SAME FORWARD MULTIPLE — BUT NOT THE SAME EARNINGS")
print("=" * 108)
print(f"{'':6s}{'fwd P/E':>10s}{'implied fwd':>14s}{'TTM GAAP':>11s}{'adjustment':>12s}{'trailing':>11s}")
print(f"{'':6s}{'(non-GAAP)':>10s}{'non-GAAP EPS':>14s}{'EPS':>11s}{'gap (x)':>12s}{'GAAP P/E':>11s}")
print("-" * 108)
for t in ("CRM", "WDAY", "MSFT", "NOW"):
    px, _, _, _, _, eps, fpe, tpe, *_ = D[t]
    fwd_eps = px / fpe
    tpe = tpe if tpe else px / eps
    print(f"{t:6s}{fpe:>10.2f}{fwd_eps:>14.2f}{eps:>11.2f}{fwd_eps/eps:>11.2f}x{tpe:>11.1f}")
print("\n  CRM  17.09x forward AND 23.1x trailing GAAP -> genuinely cheap.")
print("  WDAY 17.16x forward BUT 59.4x trailing GAAP -> the 'cheapness' is manufactured by")
print("       adding back stock comp. Same headline multiple, completely different earnings quality.")

print("\n" + "=" * 108)
print("WHAT A RE-RATING WOULD BE WORTH TO WDAY")
print("=" * 108)
wday_px, wday_fpe = D["WDAY"][0], D["WDAY"][6]
fwd_eps = wday_px / wday_fpe
print(f"  WDAY implied forward non-GAAP EPS = ${fwd_eps:.2f}\n")
for t in ("CRM", "MSFT", "NOW", "TEAM"):
    tgt = fwd_eps * D[t][6]
    print(f"  at {t}'s {D[t][6]:.1f}x forward  ->  ${tgt:>7.2f}   ({tgt/wday_px-1:>+6.1%})")
print(f"\n  Street mean target ${D['WDAY'][8]:.2f} is {D['WDAY'][8]/wday_px-1:+.1%} — BELOW the price.")
print(f"  52-week high ${D['WDAY'][10]:.2f} is {D['WDAY'][10]/wday_px-1:+.1%} away.")
