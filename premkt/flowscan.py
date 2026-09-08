"""
全市场 0DTE 期权流扫描 —— 按资金流排序，股价动量完全不参与。

和 dual.py 的问题顺序相反：
    dual      先问"谁涨得猛" -> 再问"期权好不好做"
    flowscan  先问"钱在哪条链上动" -> 股价形态只作背景

为什么需要它：META 2026-08-28 股价只涨 2.5%、按动量口径平庸，
但 0DTE ATM 成交/持仓 16x。dual.py 在预选阶段就把它砍了，
因为预选键是动量×振幅 —— 期权信号根本没机会被评估。

主排序键:
    全链成交/持仓   当日成交量 ÷ 存量持仓。>1 说明今天新建的仓位已超过
                    昨天留下的存量，整条链在被重新定价。
    C/P 成交失衡    call 成交 ÷ put 成交，衡量方向性押注的一致程度。

用法:
    python -m premkt.flowscan                 # 全市场（成交额前 100）
    python -m premkt.flowscan --top-universe 150 --min-vol 5000
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import numpy as np
import pandas as pd

from .data import now_et, quote_ctx, session_label, snapshots
from .fmt import c as _c, lj, rj, trunc
from .intraday import full_universe
from .options import expiries, target_0dte_date
from .optflow import by_strike, gamma_flip, load_chain

CFG = dict(
    top_universe=100,      # 按今日成交额取前 N 只去查有没有 0DTE
    min_turnover=2e7,      # 正股今日成交额下限
    min_chain_vol=2000,    # 全链当日成交量下限，太小的链没有分析价值
    width_pct=12.0,
    final_n=15,
)


def _flow_score(voi: float, cp: float, chain_vol: float) -> float:
    """成交/持仓比为主，方向失衡为辅，绝对成交量作规模调整。

    三者用几何平均 —— 只有换手高但方向不明、或方向极端但没量的链，
    都不该排在前面。
    """
    v = min(1.0, math.log10(max(voi, 0.05) / 0.05) / math.log10(200))   # 0.05->0, 10->1
    # 方向失衡取偏离 1.0 的程度（双向都算信号），2x 给满分
    imb = 0.0 if math.isnan(cp) or cp <= 0 else min(1.0, abs(math.log(cp)) / math.log(3.0))
    sz = min(1.0, math.log10(max(chain_vol, 100) / 100) / math.log10(2000))
    return float(100 * (max(v, 1e-6) ** 0.5) * (max(imb, 1e-6) ** 0.25) * (max(sz, 1e-6) ** 0.25))


def run(top_universe: int, min_vol: float) -> tuple[pd.DataFrame, dt.date]:
    with quote_ctx() as q:
        target = target_0dte_date(q)
        print(_c(f"\n[1/3] 全市场快照，按今日成交额取前 {top_universe} 只 …", "cyn"))
        uni = full_universe(q)
        snap = snapshots(q, uni["code"].tolist())
        d = uni.merge(snap, on="code", how="inner", suffixes=("_f", ""))
        # 列名一律避开 DataFrame 自带方法：last / squeeze / to 这类点号取值会取到方法
        d["stk_to"] = pd.to_numeric(d["turnover"], errors="coerce").fillna(0)
        d["spot_px"] = pd.to_numeric(d["last_price"], errors="coerce")
        d["prev_px"] = pd.to_numeric(d["prev_close_price"], errors="coerce")
        cand = d[(d["stk_to"] >= CFG["min_turnover"]) & (d["spot_px"] > 0)] \
                 .sort_values("stk_to", ascending=False).head(top_universe)
        print(f"      候选 {len(cand)} 只")

        print(_c(f"[2/3] 逐只查今日({target})有没有 0DTE …", "cyn"))
        has = []
        for c in cand["code"]:
            if target in expiries(q, c):
                has.append(c)
        print(f"      有 0DTE 的 {len(has)} 只: "
              + ", ".join(x.split('.')[-1] for x in has[:14])
              + (" …" if len(has) > 14 else ""))
        if not has:
            return pd.DataFrame(), target

        print(_c(f"[3/3] 拉 {len(has)} 条链并统计资金流 …", "cyn"))
        rows = []
        for c in has:
            try:
                chain, spot = load_chain(q, c, target)
                t = by_strike(chain, spot, CFG["width_pct"])
            except Exception as exc:
                print(_c(f"      {c} 跳过: {exc}", "dim"))
                continue
            if t.empty:
                continue
            cv, pv = float(t.call_vol.sum()), float(t.put_vol.sum())
            co, po = float(t.call_oi.sum()), float(t.put_oi.sum())
            chain_vol, chain_oi = cv + pv, co + po
            if chain_vol < min_vol:
                continue
            cw = t.loc[t.call_vol.idxmax()]
            pw = t.loc[t.put_vol.idxmax()]
            row = cand[cand.code == c].iloc[0]
            rows.append(dict(
                code=c, name=row.get("name", ""), spot=spot,
                change_pct=(spot / row["prev_px"] - 1) * 100 if row["prev_px"] > 0 else np.nan,
                stock_to=row["stk_to"],
                chain_vol=chain_vol, chain_oi=chain_oi,
                voi=chain_vol / chain_oi if chain_oi > 0 else np.inf,
                cp_vol=cv / pv if pv > 0 else np.inf,
                cp_oi=co / po if po > 0 else np.inf,
                call_vol=cv, put_vol=pv,
                cwall=float(cw.strike), cwall_d=(cw.strike / spot - 1) * 100,
                pwall=float(pw.strike), pwall_d=(pw.strike / spot - 1) * 100,
                flip=gamma_flip(t),
                max_strike_voi=float(t[["call_voi", "put_voi"]].replace(np.inf, np.nan).max().max()),
            ))
    w = pd.DataFrame(rows)
    if w.empty:
        return w, target
    w["score"] = [round(_flow_score(r.voi, r.cp_vol, r.chain_vol), 1) for r in w.itertuples()]
    return w.sort_values("score", ascending=False).reset_index(drop=True), target


def _n(v, nd=1):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "新链"
    for u, dv in (("M", 1e6), ("K", 1e3)):
        if abs(v) >= dv:
            return f"{v/dv:.{nd}f}{u}"
    return f"{v:.{nd}f}"


def report(w: pd.DataFrame, target: dt.date, n: int) -> None:
    if w.empty:
        print(_c("\n今天没有标的符合条件。", "yel")); return
    print(_c(f"\n0DTE 期权流榜  到期 {target}  {now_et():%m-%d %H:%M ET}"
             f"   —— 按全链成交/持仓比 + C/P 失衡排序，股价动量不参与", "bold"))
    print(_c(lj("#", 4) + lj("代码", 9) + lj("名称", 18) + rj("现价", 9) + rj("涨幅", 8)
             + rj("链成交", 9) + rj("链持仓", 9) + rj("成交/持仓", 10)
             + rj("C/P成交", 9) + rj("成交墙C", 9) + rj("距现价", 8)
             + rj("GammaFlip", 10) + rj("评分", 7), "bold"))
    print(_c("─" * 128, "dim"))
    for i, r in w.head(n).iterrows():
        voi_c = "grn" if r["voi"] >= 2 else ("yel" if r["voi"] >= 1 else "dim")
        cp_c = "grn" if r["cp_vol"] >= 2 else ("red" if r["cp_vol"] <= 0.5 else "dim")
        fl = r["flip"]
        fl_s = "n/a" if pd.isna(fl) else f"{fl:.1f}"
        fl_c = "grn" if (pd.notna(fl) and r["spot"] > fl) else "red"
        print(lj(i + 1, 4) + lj(r["code"].split(".")[-1], 9) + lj(trunc(r["name"], 16), 18)
              + rj(f"{r['spot']:.2f}", 9)
              + rj(_c(f"{r['change_pct']:+.1f}%", "grn" if r["change_pct"] > 0 else "red"), 8)
              + rj(_n(r["chain_vol"]), 9) + rj(_n(r["chain_oi"]), 9)
              + rj(_c(f"{r['voi']:.2f}x", voi_c), 10)
              + rj(_c(f"{r['cp_vol']:.2f}", cp_c), 9)
              + rj(f"{r['cwall']:.1f}", 9) + rj(f"{r['cwall_d']:+.1f}%", 8)
              + rj(_c(fl_s, fl_c), 10)
              + rj(_c(f"{r['score']:.1f}", "grn" if r["score"] >= 50 else "yel"), 7))
    print(_c("─" * 128, "dim"))
    print(_c("成交/持仓 >1 = 当日新建仓已超存量，整条链在被重新定价", "dim"))
    print(_c("C/P成交 >2 看涨押注集中 / <0.5 看跌押注集中 / 接近 1 无方向", "dim"))
    print(_c("GammaFlip 绿=现价在其上方(正GEX，波动被抑制) 红=下方(负GEX，波动被放大)", "dim"))
    print(_c("注意：成交/持仓只说明有新仓建立，看不出是买方还是卖方发起 —— "
             "快照给不到逐笔成交价靠 bid 还是 ask 的粒度", "yel"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="全市场 0DTE 期权流扫描")
    p.add_argument("--top-universe", type=int, default=CFG["top_universe"])
    p.add_argument("--min-vol", type=float, default=CFG["min_chain_vol"])
    p.add_argument("--top", type=int, default=CFG["final_n"])
    a = p.parse_args(argv)
    sess = session_label()
    print(_c(f"\n全市场 0DTE 期权流  {now_et():%Y-%m-%d %H:%M ET}  [{sess}]", "bold"))
    w, target = run(a.top_universe, a.min_vol)
    report(w, target, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
