"""
盘中动量 + 买量双升扫描。

和 scan.py（盘前）是两套逻辑：盘前只有 gap 和盘前成交额，盘中能看到
VWAP、日内位置、逐分钟资金流曲线 —— 后者才能回答"买量是不是还在上升"。

"同时上升"是硬要求，所以总分用**几何平均**而不是加权和：

    score = 100 × sqrt(动量分 × 买量分) × 流动性调整

加权和允许一头强补另一头弱（成交量暴增但价格在跌，或价格猛涨但无量），
几何平均不允许 —— 任一头接近 0，总分就接近 0。

数据来源（均为 OpenD 实时）:
    get_stock_filter   服务端粗筛。注意 US 市场只支持 CUR_PRICE / MARKET_VAL /
                       VOLUME_RATIO / CHANGE_RATE_5MIN 四个字段，CHANGE_RATE
                       不支持，所以涨幅只能靠快照拿。限频 10 次/30 秒。
    get_market_snapshot  change_rate / avg_price(当日VWAP) / high / low / 量比
    get_capital_distribution  今日累计大中小单买入 vs 卖出 -> 主力净流入
    get_capital_flow(INTRADAY)  逐分钟累计净流入曲线 -> 算斜率判断是否仍在流入

用法:
    python -m premkt.intraday                     # 默认 top 15
    python -m premkt.intraday --top 25 --min-change 3
    python -m premkt.intraday --fast              # 跳过资金流（只用量比+VWAP）
"""

from __future__ import annotations

import argparse
import math
import sys

import numpy as np
import pandas as pd
from futu import RET_OK, Market, SimpleFilter, SortDir, StockField

from .catalyst import build_catalysts
from .data import _Throttle, now_et, quote_ctx, session_label, snapshots, us_common_stocks
from .fmt import c as _c, lj, rj, trunc
from .options import target_0dte_date, zero_dte_status

# get_stock_filter 限频 10 次/30 秒，比其他接口严得多
_filter_throttle = _Throttle(max_calls=8, window=30.0)
_capital_throttle = _Throttle(max_calls=25, window=30.0)
_FULL_MODE = [False]   # 供打印函数判断是否处于全市场模式

CFG = dict(
    min_price=2.0,
    max_price=5000.0,
    min_market_cap=100_000_000.0,
    min_volume_ratio=1.3,          # 量比下限：买量必须确实放大
    min_turnover=5_000_000.0,      # 今日成交额，决定能不能真的进出
    min_change=2.0,                # 今日涨幅下限
    max_change=60.0,               # 超过就是已经走完的行情，追进去是接盘
    min_vwap_premium=0.0,          # 必须站上当日 VWAP
    min_range_pos=0.55,            # 现价在日内区间的位置，>0.55 才算强势
    flow_window=30,                # 资金流斜率的观察窗口（分钟）
    cap_split_b=10.0,             # 大/小市值分界（十亿美元）
    main_net_neutral=0.05,        # 主力净占的中性带，带内视为无方向
    universe_cap=800,
    capital_top_n=40,              # 只对前 N 名拉资金流
)


# ---------------------------------------------------------------------------
def _sf(field, lo=None, hi=None, sort=None):
    f = SimpleFilter()
    f.stock_field = field
    f.is_no_filter = False
    f.filter_min, f.filter_max, f.sort = lo, hi, sort
    return f


def screen_universe(q, cap: int) -> pd.DataFrame:
    """服务端按量比粗筛。注意榜首常年是 ADR / SPAC / 平时零成交的票 ——
    量比 1000 倍毫无意义，真正的过滤靠后面的成交额门槛。"""
    fl = [
        _sf(StockField.CUR_PRICE, CFG["min_price"], CFG["max_price"]),
        _sf(StockField.MARKET_VAL, CFG["min_market_cap"]),
        _sf(StockField.VOLUME_RATIO, CFG["min_volume_ratio"], None, SortDir.DESCEND),
    ]
    rows, begin = [], 0
    while begin < cap:
        _filter_throttle.wait()
        ret, data = q.get_stock_filter(Market.US, filter_list=fl, begin=begin, num=200)
        if ret != RET_OK:
            if not rows:
                raise RuntimeError(f"get_stock_filter failed: {data}")
            break
        last_page, all_count, lst = data
        if not lst:
            break
        rows += [dict(code=x.stock_code, name=x.stock_name,
                      cur_price=x.cur_price, market_val=x.market_val,
                      volume_ratio=x.volume_ratio) for x in lst]
        begin += len(lst)
        if last_page or begin >= all_count:
            break
    return pd.DataFrame(rows)


def full_universe(q) -> pd.DataFrame:
    """全市场普通股，不做任何服务端预筛。

    为什么需要它：get_stock_filter 在 US 市场只支持 CUR_PRICE / MARKET_VAL /
    VOLUME_RATIO / CHANGE_RATE_5MIN 四个字段，**不支持 TURNOVER**。
    用 VOLUME_RATIO≥1.3 当 universe 会把"成交额巨大但量比正常"的权重股
    整个排除掉 —— 要按成交额排名就必须自己快照全市场再排。
    13k 只 ≈ 65 次快照调用，节流后约 1 分钟，可以接受。
    """
    basic = us_common_stocks(q)
    keep = basic[basic["exchange_type"].isin(
        {"US_NASDAQ", "US_NYSE", "US_AMEX", "US_NYSE_AMERICAN", "US_ARCA"})]
    delist = keep.get("delisting")
    if delist is not None:
        keep = keep[~delist.fillna(False).astype(bool)]
    return keep[["code", "name"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
def capital_signals(q, code: str, window: int) -> dict:
    """主力净流入占比 + 近 window 分钟的净流入斜率。

    capital_distribution 是今日累计的买入/卖出拆单统计；
    capital_flow(INTRADAY) 是逐分钟的累计净流入曲线，用它的末端斜率
    判断资金是"还在进"还是"已经开始撤"。
    """
    out = dict(main_net=np.nan, main_net_ratio=np.nan,
               flow_now=np.nan, flow_slope=np.nan, flow_rising=False)

    _capital_throttle.wait()
    try:
        ret, d = q.get_capital_distribution(code)
    except Exception:
        ret = -1
    if ret == RET_OK and d is not None and len(d):
        r = d.iloc[0]
        # 主力 = 特大单 + 大单
        buy = float(r["capital_in_super"]) + float(r["capital_in_big"])
        sell = float(r["capital_out_super"]) + float(r["capital_out_big"])
        total = buy + sell
        out["main_net"] = buy - sell
        out["main_net_ratio"] = (buy - sell) / total if total > 0 else np.nan

    _capital_throttle.wait()
    try:
        ret2, f = q.get_capital_flow(code, period_type="INTRADAY")
    except Exception:
        ret2 = -1
    if ret2 == RET_OK and f is not None and len(f) > 2:
        s = pd.to_numeric(f["in_flow"], errors="coerce").dropna()
        # 末尾常有重复的一行（同一分钟推两次），去掉不影响斜率
        s = s[~s.duplicated(keep="first")] if len(s) > 3 else s
        if len(s) >= 3:
            out["flow_now"] = float(s.iloc[-1])
            n = min(window, len(s) - 1)
            out["flow_slope"] = float(s.iloc[-1] - s.iloc[-1 - n]) / n
            out["flow_rising"] = out["flow_slope"] > 0
    return out


# ---------------------------------------------------------------------------
def _momentum_score(change: float, range_pos: float, vwap_prem: float) -> float:
    """动量：涨幅 + 日内位置 + 相对 VWAP 的溢价。

    涨幅用对数正态曲线（峰值 8%）而不是线性 —— 当日已涨 40% 的票，
    剩下的空间通常小于风险。
    """
    if change <= 0:
        return 0.0
    g = math.exp(-((math.log(max(change, 0.1) / 8.0)) ** 2) / (2 * 0.95**2))
    p = max(0.0, min(1.0, (range_pos - 0.4) / 0.55))        # 0.4→0, 0.95→1
    v = max(0.0, min(1.0, vwap_prem / 3.0))                 # 高出 VWAP 3% 满分
    return float(0.45 * g + 0.35 * p + 0.20 * v)


def _buyvol_score(vol_ratio: float, main_ratio: float, slope_ratio: float,
                  bid_ask: float) -> float:
    """买量：量比 + 主力净流入占比 + 净流入斜率 + 买卖盘比。"""
    parts, wts = [], []

    if not math.isnan(vol_ratio) and vol_ratio > 0:
        parts.append(min(1.0, math.log10(max(vol_ratio, 1.0)) / math.log10(8.0)))
        wts.append(0.35)

    if not math.isnan(main_ratio):
        parts.append(max(0.0, min(1.0, (main_ratio + 0.1) / 0.35)))   # -0.1→0, 0.25→1
        wts.append(0.30)

    if not math.isnan(slope_ratio):
        # 斜率按占今日成交额的比例归一：每分钟净流入占成交额 0.15% 即满分
        parts.append(max(0.0, min(1.0, slope_ratio / 0.0015)))
        wts.append(0.25)

    if not math.isnan(bid_ask) and bid_ask > 0:
        parts.append(max(0.0, min(1.0, (bid_ask - 0.6) / 1.0)))
        wts.append(0.10)

    if not parts:
        return 0.0
    return float(np.average(parts, weights=wts))


def _liquidity_adj(turnover: float, spread_pct: float) -> float:
    a = 1.0 if turnover >= 5e7 else (0.95 if turnover >= 2e7 else
                                     (0.88 if turnover >= 1e7 else 0.80))
    if not math.isnan(spread_pct):
        if spread_pct > 1.0:
            a *= 0.75
        elif spread_pct > 0.4:
            a *= 0.92
    return a


# ---------------------------------------------------------------------------
RANK_KEYS = {"score": ("prelim", "score", "动量×买量评分"),
             "turnover": ("turnover", "turnover", "成交额"),
             "change": ("change_pct", "change_pct", "涨幅")}


def run(top: int = 15, use_capital: bool = True, use_news: bool = False,
        split_cap: float | None = None, per_bucket: int = 7,
        full: bool = False, rank_by: str = "score",
        use_odte: bool = False) -> pd.DataFrame:
    sess = session_label()
    _FULL_MODE[0] = full
    with quote_ctx() as q:
        if full:
            print(_c("\n[1/4] 取全市场普通股（不预筛，按成交额排名必须全覆盖）…", "cyn"))
            uni = full_universe(q)
            print(f"      {len(uni)} 只，约需 {len(uni)//200 + 1} 次快照调用")
        else:
            print(_c("\n[1/4] 服务端粗筛（量比 / 价格 / 市值）…", "cyn"))
            uni = screen_universe(q, CFG["universe_cap"])
            print(f"      量比 ≥ {CFG['min_volume_ratio']} 的有 {len(uni)} 只")
            if uni.empty:
                return pd.DataFrame()
            basic = us_common_stocks(q)
            common = set(basic[basic["exchange_type"].isin(
                {"US_NASDAQ", "US_NYSE", "US_AMEX", "US_NYSE_AMERICAN", "US_ARCA"})]["code"])
            uni = uni[uni["code"].isin(common)]
            print(f"      剔除 ETF / 权证 / 粉单后 {len(uni)} 只")
        if uni.empty:
            return pd.DataFrame()

        print(_c("[2/4] 拉快照（涨幅 / VWAP / 日内位置 / 买卖盘）…", "cyn"))
        snap = snapshots(q, uni["code"].tolist())
        df = uni.merge(snap, on="code", how="inner", suffixes=("_f", ""))

        rows = []
        for _, r in df.iterrows():
            last = float(r.get("last_price") or 0)
            hi, lo = float(r.get("high_price") or 0), float(r.get("low_price") or 0)
            vwap = float(r.get("avg_price") or 0)
            # 快照没有 change_rate 字段，今日涨幅得自己算
            prev = float(r.get("prev_close_price") or 0)
            chg = (last / prev - 1) * 100 if prev > 0 and last > 0 else 0.0
            to = float(r.get("turnover") or 0)
            ask, bid = float(r.get("ask_price") or 0), float(r.get("bid_price") or 0)
            mid = (ask + bid) / 2
            rows.append(dict(
                code=r["code"], name=r.get("name", ""),
                last=last, change_pct=chg, turnover=to,
                volume_ratio=float(r.get("volume_ratio") or 0),
                vwap=vwap,
                vwap_premium=(last / vwap - 1) * 100 if vwap > 0 else np.nan,
                range_pos=(last - lo) / (hi - lo) if hi > lo else np.nan,
                high=hi, low=lo,
                bid_ask_ratio=float(r.get("bid_ask_ratio") or np.nan),
                spread_pct=(ask - bid) / mid * 100 if mid > 0 else np.nan,
                market_cap=float(r.get("total_market_val") or np.nan),
                float_shares=float(r.get("outstanding_shares") or np.nan),
            ))
        w = pd.DataFrame(rows)

        # --- 硬门槛：动量和买量必须"同时"成立 ---
        before = len(w)
        # 涨幅上限是个会静默吞掉当日最大成交额个股的门槛，必须显式报出来
        hot = w[(w["change_pct"] > CFG["max_change"])
                & (w["turnover"] >= CFG["min_turnover"])]
        excluded_hot = [(r["code"].split(".")[-1], r["change_pct"])
                        for _, r in hot.sort_values("turnover", ascending=False).iterrows()]
        w = w[
            (w["change_pct"] >= CFG["min_change"])
            & (w["change_pct"] <= CFG["max_change"])
            & (w["turnover"] >= CFG["min_turnover"])
            & (w["volume_ratio"] >= (0.0 if full else CFG["min_volume_ratio"]))
            & (w["vwap_premium"] > CFG["min_vwap_premium"])
            & (w["range_pos"] >= CFG["min_range_pos"])
        ].copy()
        print(f"      过双升门槛 {len(w)} / {before} "
              f"({CFG['min_change']}%≤涨幅≤{CFG['max_change']}% 且 站上VWAP 且 "
              f"日内位置≥{CFG['min_range_pos']:.0%} 且 量比≥{CFG['min_volume_ratio']} "
              f"且 成交额≥${CFG['min_turnover']/1e6:.0f}M)")
        if len(excluded_hot):
            names = ", ".join(f"{c}({p:+.0f}%)" for c, p in excluded_hot[:5])
            print(_c(f"      注意：{len(excluded_hot)} 只因涨幅 >{CFG['max_change']}% 被剔除: {names}"
                     f"{' …' if len(excluded_hot) > 5 else ''}  —— 用 --max-change 放开", "yel"))
        if w.empty:
            return pd.DataFrame()

        # 先用无资金流的分数排序，决定谁值得拉资金流
        w["prelim"] = [
            _momentum_score(r["change_pct"], r["range_pos"], r["vwap_premium"])
            * _buyvol_score(r["volume_ratio"], np.nan, np.nan, r["bid_ask_ratio"])
            for _, r in w.iterrows()
        ]
        w = w.sort_values("prelim", ascending=False).reset_index(drop=True)

        # 按市值分桶，各桶内按"今日成交额"取前 N —— 用户要的是"交易量最多"的，
        # 所以排序键是 turnover 而不是打分；打分只用来过动量门槛。
        if split_cap is not None:
            line = split_cap * 1e9
            w["bucket"] = np.where(w["market_cap"] >= line, "大市值", "小市值")
            presel, _, label = RANK_KEYS[rank_by]
            w = (w.groupby("bucket", group_keys=False)
                   .apply(lambda g: g.sort_values(presel, ascending=False).head(per_bucket))
                   .reset_index(drop=True))
            print(f"      分桶后 {len(w)} 只（分界 ${split_cap:.0f}B，各取{label}前 {per_bucket}）")

        if use_capital:
            head = w if split_cap is not None else w.head(CFG["capital_top_n"])
            print(_c(f"[3/4] 拉资金流（{len(head)} 只：主力净流入 + 近"
                     f"{CFG['flow_window']}分钟净流入斜率）…", "cyn"))
            sig = [capital_signals(q, c, CFG["flow_window"]) for c in head["code"]]
            s = pd.DataFrame(sig, index=head.index)
            w = w.join(s)
        else:
            print(_c("[3/4] 跳过资金流（--fast）", "dim"))
            for k in ("main_net", "main_net_ratio", "flow_now", "flow_slope"):
                w[k] = np.nan
            w["flow_rising"] = False

    print(_c("[4/4] 打分（动量 × 买量 的几何平均）…", "cyn"))
    if use_capital:
        # 超出 capital_top_n 的没有资金数据，无法判断"是否仍在流入"
        w = w[w["main_net_ratio"].notna()].copy()
    recs = []
    for _, r in w.iterrows():
        slope_ratio = (r.get("flow_slope", np.nan) / r["turnover"]
                       if r["turnover"] > 0 and not pd.isna(r.get("flow_slope")) else np.nan)
        m = _momentum_score(r["change_pct"], r["range_pos"], r["vwap_premium"])
        b = _buyvol_score(r["volume_ratio"], r.get("main_net_ratio", np.nan),
                          slope_ratio, r["bid_ask_ratio"])
        adj = _liquidity_adj(r["turnover"], r["spread_pct"])
        recs.append(dict(m_score=round(m, 3), b_score=round(b, 3),
                         slope_ratio=slope_ratio,
                         score=round(100 * math.sqrt(max(m, 0) * max(b, 0)) * adj, 1)))
    w = pd.concat([w.reset_index(drop=True), pd.DataFrame(recs)], axis=1)

    if use_capital:
        # "同时上升"的最后一道：主力资金没在出货，且近 30 分钟仍在净流入。
        # main_net_ratio 用 ±5% 的中性带 —— 大单买卖的分类本身有噪声，
        # -4% 和 -35% 是完全不同的性质，用 >0 一刀切会把前者误判成出货。
        neutral = CFG["main_net_neutral"]
        w["both_rising"] = (w["main_net_ratio"] > -neutral) & (w["flow_slope"] > 0)
        w["distributing"] = (w["main_net_ratio"] <= -neutral) | (w["flow_slope"] <= 0)
    else:
        w["both_rising"] = True

    if split_cap is not None:
        _, final_key, _ = RANK_KEYS[rank_by]
        w = w.sort_values(["bucket", final_key], ascending=[True, False]).reset_index(drop=True)
    else:
        w = w.sort_values(["both_rising", "score"], ascending=[False, False]).reset_index(drop=True)
        w = w.head(top)

    if use_news and not w.empty:
        print(_c(f"[5/5] 查催化剂（{len(w)} 只：SEC 8-K + moomoo 新闻）…", "cyn"))
        with quote_ctx() as q:
            cats = build_catalysts(q, w[["code", "name"]].to_dict("records"))
        w["catalyst_kind"] = [cats[c].kind if c in cats else "" for c in w["code"]]
        w["catalyst_label"] = [cats[c].label_for("long") if c in cats else "none"
                               for c in w["code"]]
        w["catalyst_evidence"] = [cats[c].evidence if c in cats else [] for c in w["code"]]

    if use_odte and not w.empty:
        print(_c(f"[6/6] 查 0DTE 期权到期日（{len(w)} 只）…", "cyn"))
        with quote_ctx() as q:
            target = target_0dte_date(q)
            st = zero_dte_status(q, w["code"].tolist(), target) if target else {}
        w.attrs["odte_target"] = target
        w["has_options"] = [st.get(c, {}).get("has_options", False) for c in w["code"]]
        w["has_0dte"] = [st.get(c, {}).get("has_0dte", False) for c in w["code"]]
        w["nearest_expiry"] = [st.get(c, {}).get("nearest") for c in w["code"]]
        w["dte"] = [st.get(c, {}).get("dte") for c in w["code"]]

    w.attrs["session"] = sess
    return w


# ---------------------------------------------------------------------------
def _rows(df: pd.DataFrame, start: int, show_news: bool) -> None:
    for i, r in df.iterrows():
        mn = "n/a" if pd.isna(r["main_net_ratio"]) else f"{r['main_net_ratio']*100:+.0f}%"
        mn_c = "grn" if (r["main_net_ratio"] or 0) > 0 else "red"
        fs = "n/a" if pd.isna(r["flow_slope"]) else _money(r["flow_slope"] * CFG["flow_window"])
        fs_c = "grn" if (r["flow_slope"] or 0) > 0 else "red"
        sc_c = "grn" if r["score"] >= 55 else ("yel" if r["score"] >= 40 else "dim")
        line = (lj(start + i + 1, 4) + lj(r["code"], 10) + lj(trunc(r["name"], 18), 20)
                + rj(f"{r['last']:.2f}", 9)
                + rj(_c(f"{r['change_pct']:+.1f}%", "grn"), 8)
                + rj(f"{r['volume_ratio']:.1f}", 7)
                + rj(_money(r["turnover"]), 9)
                + rj(f"{r['vwap_premium']:+.1f}%", 8)
                + rj(f"{r['range_pos']*100:.0f}%", 7)
                + rj(_c(mn, mn_c), 9) + rj(_c(fs, fs_c), 9)
                + rj(f"{r['m_score']:.2f}", 6) + rj(f"{r['b_score']:.2f}", 6)
                + rj(_c(f"{r['score']:.1f}", sc_c), 7))
        if show_news:
            line += "  " + lj(trunc(r.get("catalyst_kind", ""), 16), 18)
        print(line)


def print_table(df: pd.DataFrame, show_news: bool = False) -> None:
    if df.empty:
        print(_c("\n没有标的同时满足动量与买量双升。", "yel"))
        return

    header = (lj("#", 4) + lj("代码", 10) + lj("名称", 20) + rj("现价", 9)
              + rj("涨幅", 8) + rj("量比", 7) + rj("成交额", 9) + rj("离VWAP", 8)
              + rj("日内位", 7) + rj("主力净占", 9) + rj("30m流入", 9)
              + rj("动量", 6) + rj("买量", 6) + rj("评分", 7))
    if show_news:
        header += "  " + lj("催化剂", 18)
    width = 126 + (18 if show_news else 0)

    rising = df[df["both_rising"]].reset_index(drop=True)
    fading = df[~df["both_rising"]].reset_index(drop=True)

    # 门槛的措辞必须和 both_rising 的实际算法一致：主力净占用的是 ±5% 中性带，
    # 不是 >0。写成"为正"会让 -2% 的票看起来自相矛盾。
    print(_c(f"\n双升（动量↑ + 买量↑，主力未出货[净占>-{CFG['main_net_neutral']*100:.0f}%] "
             f"且近{CFG['flow_window']}分钟仍在净流入）", "bold"))
    print(_c(header, "bold"))
    print(_c("─" * width, "dim"))
    if rising.empty:
        print(_c("  （无）", "yel"))
    else:
        _rows(rising, 0, show_news)
    print(_c("─" * width, "dim"))

    if not fading.empty:
        print(_c(f"\n⚠ 涨势尚在但资金已转流出 —— 主力净占 ≤ -{CFG['main_net_neutral']*100:.0f}% "
                 f"或近 {CFG['flow_window']} 分钟净流入为负，不满足双升条件", "yel"))
        _rows(fading, len(rising), show_news)
        print(_c("─" * width, "dim"))

    print(_c("主力净占 = (特大单+大单)净买入 / 该档总成交   30m流入 = 近30分钟累计净流入", "dim"))
    print(_c("评分 = 100 × √(动量 × 买量) × 流动性调整 —— 几何平均，任一头弱则总分低", "dim"))


def _money(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= d:
            return f"{v/d:.1f}{u}"
    return f"{v:.0f}"


def _odte_cell(r) -> str:
    """0DTE 一列：有当日到期 / 有期权但最近到期还有 N 天 / 没有期权。"""
    if "has_0dte" not in r.index:
        return ""
    if not r.get("has_options"):
        return _c("无期权", "dim")
    if r.get("has_0dte"):
        return _c("✓ 0DTE", "grn")
    d = r.get("dte")
    return _c(f"最近+{int(d)}d" if pd.notna(d) else "—", "dim")


def print_buckets(df: pd.DataFrame, show_news: bool = False,
                  rank_label: str = "今日成交额") -> None:
    """按市值分组输出，组内按指定键降序。"""
    if df.empty:
        print(_c("\n没有标的通过动量门槛。", "yel"))
        return
    show_odte = "has_0dte" in df.columns
    header = (lj("#", 4) + lj("代码", 10) + lj("名称", 20) + rj("现价", 9)
              + rj("涨幅", 8) + rj("成交额", 10) + rj("量比", 7) + rj("离VWAP", 8)
              + rj("日内位", 7) + rj("市值", 9) + rj("主力净占", 9)
              + rj("30m流入", 9) + rj("评分", 7))
    if show_odte:
        header += rj("0DTE", 10)
    if show_news:
        header += "  " + lj("催化剂", 18)
    width = 126 + (10 if show_odte else 0) + (18 if show_news else 0)

    for bucket in ("大市值", "小市值"):
        g = df[df["bucket"] == bucket].reset_index(drop=True)
        if g.empty:
            continue
        line = CFG["cap_split_b"]
        desc = f"≥ ${line:.0f}B" if bucket == "大市值" else f"< ${line:.0f}B"
        print(_c(f"\n{bucket}（{desc}）· 按{rank_label}排序", "bold"))
        print(_c(header, "bold"))
        print(_c("─" * width, "dim"))
        for i, r in g.iterrows():
            mn = "n/a" if pd.isna(r.get("main_net_ratio")) else f"{r['main_net_ratio']*100:+.0f}%"
            mn_c = "grn" if (r.get("main_net_ratio") or 0) > 0 else "red"
            fs = ("n/a" if pd.isna(r.get("flow_slope"))
                  else _money(r["flow_slope"] * CFG["flow_window"]))
            fs_c = "grn" if (r.get("flow_slope") or 0) > 0 else "red"
            sc_c = "grn" if r["score"] >= 55 else ("yel" if r["score"] >= 40 else "dim")
            row = (lj(i + 1, 4) + lj(r["code"], 10) + lj(trunc(r["name"], 18), 20)
                   + rj(f"{r['last']:.2f}", 9)
                   + rj(_c(f"{r['change_pct']:+.1f}%", "grn"), 8)
                   + rj(_money(r["turnover"]), 10)
                   + rj(f"{r['volume_ratio']:.1f}", 7)
                   + rj(f"{r['vwap_premium']:+.1f}%", 8)
                   + rj(f"{r['range_pos']*100:.0f}%", 7)
                   + rj(_money(r["market_cap"]), 9)
                   + rj(_c(mn, mn_c), 9) + rj(_c(fs, fs_c), 9)
                   + rj(_c(f"{r['score']:.1f}", sc_c), 7))
            if show_odte:
                row += rj(_odte_cell(r), 10)
            if show_news:
                row += "  " + lj(trunc(r.get("catalyst_kind", ""), 16), 18)
            mnr = r.get("main_net_ratio")
            if pd.notna(mnr) and mnr <= -CFG["main_net_neutral"]:
                row += _c(" ↓大单出货", "red")
            elif not r.get("both_rising", True):
                row += _c(" ·流入放缓", "yel")
            print(row)
        print(_c("─" * width, "dim"))
    vr = "" if _FULL_MODE[0] else f" 且 量比≥{CFG['min_volume_ratio']}"
    print(_c(f"门槛：{CFG['min_change']}%≤涨幅≤{CFG['max_change']}% 且 站上VWAP 且 "
             f"日内位置≥{CFG['min_range_pos']:.0%}{vr} 且 成交额≥${CFG['min_turnover']/1e6:.0f}M"
             + ("（全市场模式，量比不设门槛 —— 权重股量比常年 <1.3）" if _FULL_MODE[0] else ""), "dim"))
    print(_c("↓大单出货 = 主力净占 ≤ -5%（真在卖）  ·流入放缓 = 近30分钟净流入转负", "dim"))
    print(_c("主力净占在 ±5% 内视为噪声，不作方向判断", "dim"))
    if "has_0dte" in df.columns:
        tgt = df.attrs.get("odte_target")
        _when = "今天" if session_label() in ("premarket", "regular") else "下一个交易日"
        print(_c(f"0DTE 指 {tgt} 当天到期的期权（{_when}）。"
                 f"单票到期日已扩到周一/三/五，但多数中小盘仍是仅周五 —— 逐只实测，不靠惯例",
                 "dim"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="盘中动量+买量双升扫描")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--min-change", type=float, help="今日涨幅下限 %%")
    p.add_argument("--min-volume-ratio", type=float, help="量比下限")
    p.add_argument("--min-turnover", type=float, help="今日成交额下限（美元）")
    p.add_argument("--max-change", type=float,
                   help="今日涨幅上限 %%（默认 60，防止追已走完的行情）。"
                        "按成交额找票时建议放开 —— 当日成交额最大的常常就是极端票")
    p.add_argument("--fast", action="store_true", help="跳过资金流查询")
    p.add_argument("--news", action="store_true", help="附带催化剂查询（SEC 8-K + 新闻）")
    p.add_argument("--split-cap", type=float, nargs="?", const=10.0,
                   help="按市值分大/小两组，各取成交额前 N（默认分界 $10B）")
    p.add_argument("--per-bucket", type=int, default=7, help="每组取几只")
    p.add_argument("--full", action="store_true",
                   help="快照全市场再排名（按成交额选股必须用，否则会漏掉量比正常的权重股）")
    p.add_argument("--rank-by", choices=["score", "turnover", "change"], default="score",
                   help="组内排序键：score=动量×买量评分(默认) / turnover=成交额 / change=涨幅")
    p.add_argument("--odte", action="store_true",
                   help="查每只票下一个交易日有没有 0DTE 期权")
    a = p.parse_args(argv)
    for k, v in (("min_change", a.min_change), ("min_volume_ratio", a.min_volume_ratio),
                 ("min_turnover", a.min_turnover), ("max_change", a.max_change)):
        if v is not None:
            CFG[k] = v

    sess = session_label()
    note = {"regular": _c("✓ 盘中，数据实时", "grn"),
            "premarket": _c("⚠ 盘前 —— 日内字段尚未开始累积，建议用 premkt.scan", "yel"),
            "afterhours": _c("⚠ 盘后 —— 日内字段停在收盘值", "yel"),
            "overnight": _c("夜盘时段 —— 日内字段是最近一个已收盘交易日的完整数据（可用于当日复盘）", "yel"),
            "closed": _c("⚠ 休市 —— 日内字段停留在最近一个交易日", "yel")}[sess]
    print(_c(f"\n盘中动量+买量双升  {now_et():%Y-%m-%d %H:%M ET}  ", "bold") + note)

    df = run(top=a.top, use_capital=not a.fast, use_news=a.news,
             split_cap=a.split_cap, per_bucket=a.per_bucket,
             full=a.full or a.split_cap is not None,
             rank_by=a.rank_by, use_odte=a.odte)
    if a.split_cap is not None:
        print_buckets(df, show_news=a.news, rank_label=RANK_KEYS[a.rank_by][2])
    else:
        print_table(df, show_news=a.news)
    return 0


if __name__ == "__main__":
    sys.exit(main())
