"""
0DTE / 期权可交易性检查。

判断"有没有 0DTE"不能靠记忆或惯例 —— 单票的到期日安排这几年一直在扩。
实测（2026-08-25）:
    SPY/QQQ/IWM  每个交易日都有到期
    TSLA/AAPL/AMD  周一/三/五
    NVDA         最近是 08-28，跳过了 08-26
    SOFI         只有周五
所以只能逐只查真实到期日列表。

交易日历也用同样的思路：SPY 每个交易日都有一个到期日，
所以 SPY 的到期日集合 == 交易日集合，比自己维护假期表可靠。
"""

from __future__ import annotations

import datetime as dt

from futu import RET_OK

from .data import _Throttle, now_et, session_label

# get_option_chain 限频 10 次/30 秒（get_option_expiration_date 也走同一档）。
# 设快了后续调用会直接失败，而失败常被当成"没有期权"静默跳过 —— 实测 MCD
# 的 6 个 LEAPS 到期日就是这样整体消失的。
_opt_throttle = _Throttle(max_calls=9, window=30.0)

CALENDAR_PROXY = "US.SPY"


def _to_date(s) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def expiries(q, code: str) -> list[dt.date]:
    """某标的的全部期权到期日（升序）。没有期权则返回空列表。"""
    _opt_throttle.wait()
    try:
        ret, d = q.get_option_expiration_date(code)
    except Exception:
        return []
    if ret != RET_OK or d is None or len(d) == 0:
        return []
    col = "strike_time" if "strike_time" in d.columns else d.columns[0]
    out = [_to_date(x) for x in d[col]]
    return sorted(x for x in out if x is not None)


def target_0dte_date(q, ref: dt.datetime | None = None) -> dt.date | None:
    """0DTE 指哪一天：盘中就是今天，收盘后就是下一个交易日。

    用 SPY 的到期日当交易日历 —— 取 >= 目标日的最早一个 SPY 到期日。
    """
    ref = ref or now_et()
    sess = session_label(ref)
    want = ref.date() if sess in ("premarket", "regular") else ref.date() + dt.timedelta(days=1)
    cal = expiries(q, CALENDAR_PROXY)
    for x in cal:
        if x >= want:
            return x
    return None


def zero_dte_status(q, codes: list[str], target: dt.date) -> dict[str, dict]:
    """逐只判断有没有 target 当天到期的期权，并给出最近的替代到期日。"""
    out: dict[str, dict] = {}
    for c in codes:
        exps = expiries(q, c)
        future = [x for x in exps if x >= target]
        out[c] = dict(
            has_options=bool(exps),
            has_0dte=target in exps,
            nearest=future[0] if future else None,
            dte=(future[0] - target).days if future else None,
            n_expiries=len(exps),
        )
    return out


def atm_liquidity(q, code: str, expiry: dt.date, spot: float) -> dict:
    """取该到期日 ATM 附近合约的成交量/持仓/隐波/点差，判断 0DTE 是否真的能交易。

    有 0DTE 挂牌 ≠ 能交易。小票的 0DTE 常年零成交、点差比权利金还宽。
    """
    empty = dict(atm_strike=None, call_vol=None, call_oi=None, call_iv=None,
                 call_spread_pct=None, tradeable=False)
    _opt_throttle.wait()
    try:
        ret, chain = q.get_option_chain(code, start=expiry.strftime("%Y-%m-%d"),
                                        end=expiry.strftime("%Y-%m-%d"),
                                        option_type="CALL")
    except Exception:
        return empty
    if ret != RET_OK or chain is None or len(chain) == 0:
        return empty

    chain = chain.copy()
    chain["_k"] = chain["strike_price"].astype(float)
    chain["_d"] = (chain["_k"] - spot).abs()
    row = chain.sort_values("_d").iloc[0]

    from .data import snapshots
    snap = snapshots(q, [row["code"]], quiet=True)
    if snap.empty:
        return {**empty, "atm_strike": float(row["_k"])}

    s = snap.iloc[0]
    ask = float(s.get("ask_price") or 0)
    bid = float(s.get("bid_price") or 0)
    mid = (ask + bid) / 2
    vol = float(s.get("volume") or 0)
    oi = float(s.get("option_open_interest") or 0)
    spread_pct = (ask - bid) / mid * 100 if mid > 0 else None

    return dict(
        atm_strike=float(row["_k"]),
        call_vol=vol,
        call_oi=oi,
        call_iv=float(s.get("option_implied_volatility") or 0) or None,
        call_spread_pct=spread_pct,
        # 能交易的门槛：有成交、有持仓、点差不至于吃掉全部预期收益
        tradeable=bool(vol >= 100 and oi >= 100 and (spread_pct is not None and spread_pct <= 25)),
    )
