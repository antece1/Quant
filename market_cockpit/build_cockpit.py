#!/usr/bin/env python3
"""
市场风险驾驶舱 — 构建入口

用法:
    python3 build_cockpit.py                  # 用今天日期取数并生成 HTML
    python3 build_cockpit.py --date 2026-07-29
    python3 build_cockpit.py --dry-run        # 只打印评分，不写文件

产物: market_cockpit.html （单文件，可直接浏览器打开）

依赖: 本地 moomoo OpenD 必须在 127.0.0.1:11111 运行且已登录。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import fetch_data as F

HERE = pathlib.Path(__file__).parent
TEMPLATE = HERE / "cockpit_template.html"
OUTPUT = HERE / "market_cockpit.html"

# ==========================================================================
# 人工输入区 —— 这些值无法从 OpenD 推导，必须手工维护并注明依据与日期。
# 每次更新请同步改 `source` 说明，页面会把它显示出来。
# ==========================================================================
MANUAL = {
    # 联邦基金目标利率区间
    "fed_rate_low": 3.50,
    "fed_rate_high": 3.75,
    # 政策立场分: -100 极鹰 .. +100 极鸽
    "fed_policy_stance": -65,
    "fed_stance_reason_en": (
        "Jackson Hole, Aug 28: Warsh said \"the Fed's predominant focus right now "
        "should be on prices,\" called 2% \"a firm, fixed target,\" warned that "
        "\"inflation is not necessarily mean-reverting,\" and closed with \"we must be "
        "confident that underlying inflation is moving to our objective... otherwise, "
        "we have work to do\" — a clear signal hikes are on the table. He cited PCE at "
        "3.7% over 12 months but 4.1% over 6 months, i.e. re-accelerating, and rejected "
        "forward guidance as a \"hall-of-mirrors problem.\" September hike odds jumped "
        "from ~35% to ~62% over the weekend and several large banks pulled forward their "
        "hike timing. Backdrop: Jul 29 FOMC held 9–3 with all three dissenters wanting a "
        "hike, and the Aug 30 US strike on Iranian launchers near Hormuz has pushed Brent "
        "back above $90, adding an energy-price impulse. "
        "Sep 4 update: eased from -75 to -65. Governor Waller signalled openness to "
        "holding if disinflation resumes, pulling market-implied September hike odds back "
        "from ~62% to ~50%. The August jobs report beat hard (162K vs 55K) but "
        "unemployment held at 4.1% and hourly earnings were in line, so it did not force "
        "the issue. The Chair's hawkish framing stands; the certainty around it does not — "
        "next week's CPI (Sep 11) and PPI (Sep 10) are the actual deciders."
    ),
    "fed_stance_reason": (
        "8/28 Jackson Hole：Warsh 称「美联储当前的首要焦点应是物价」，重申 2% 是"
        "「坚定不移的固定目标」，警告「通胀未必会自行回归均值」，并以"
        "「我们必须确信潜在通胀正朝目标迈进……否则我们还有工作要做」收尾 —— "
        "明确释放加息可能性。他引用 PCE 12个月 3.7%、但 6个月年化 4.1%，即正在重新加速；"
        "同时以「镜厅问题」为由否定前瞻指引。周末 9 月加息概率由约 35% 跳升至约 62%，"
        "多家大行提前了加息时点预期。背景：7/29 会议 9–3 票维持且三张反对票全部主张加息；"
        "8/30 美军空袭霍尔木兹附近伊朗发射装置，布伦特重回 $90 上方，叠加能源价格冲击。"
        "9/4 更新：由 -75 上调至 -65。理事 Waller 表示若反通胀重启则倾向按兵不动，"
        "市场定价的 9 月加息概率由 ~62% 回落至 ~50%。8 月非农大超预期（16.2万 vs 预期 5.5万），"
        "但失业率维持 4.1%、时薪符合预期，未构成加息的决定性理由。"
        "主席的鹰派框架未变，但围绕它的确定性下降 —— 真正的决定权在下周的 "
        "CPI（9/11）与 PPI（9/10）。"
    ),
    "fed_source_en": "federalreserve.gov speech warsh20260828a + Waller remarks 2026-09-04 + market-implied Sept hike odds ~50% + FOMC 2026-07-29 vote tally",
    "fed_source": "federalreserve.gov 讲话 warsh20260828a + Waller 2026-09-04 表态 + 市场定价 9 月加息概率 ~50% + FOMC 2026-07-29 票型",
    # 注：立场分只跟随「美联储实际表态」变动，不因数据流自行调整 ——
    # 数据流由自动的宏观意外因子负责。
    # 2026-08-28：Warsh 首次 Jackson Hole 演讲明确把加息摆上台面，
    # 市场定价的 9 月加息概率跳至 ~62%，立场分由 -55 下调至 -75。
    # 2026-09-04：Waller 鸽派表态令加息概率回落至 ~50%，立场分回调至 -65。
    # 下次可更新节点：9/10 PPI、9/11 CPI、9/15–16 FOMC 决议。
    #
    # 下次 FOMC 决议日（7/29 会议已结束，下次为 9/15–16，决议在第二日）
    "next_fomc": "2026-09-16",
    # 真实 VIX 点位（Futu 取不到 VIX 指数，需外部填；留 None 则用 SPY 已实现波动率代理）
    "vix": 15.30,
    "vix_source_en": "CBOE VIX 15.30 on 2026-09-08, +5.30% on the day — lifting off the 08-28 year-to-date low of 14.43",
    "vix_source": "CBOE VIX 2026-09-08 报 15.30，当日 +5.30% —— 自 08-28 年内低点 14.43 抬升",
    # 真实原油现货/期货报价。Futu 只有 USO/BNO 这类 ETF（跟踪期货、有滚动
    # 损耗），不等于油价，故真实报价须手工维护并注明日期。
    "brent_spot": 97.29,
    "wti_spot": 92.97,
    "oil_spot_source_en": "Brent $97.29 (2026-09-07, +1.05%, a near seven-week high) / WTI $92.97 — Brent rose 9.3% last week. US and Iran struck each other's tankers over the weekend and Tehran plans a maritime exclusion zone outside Hormuz; Goldman flags $120 risk",
    "oil_spot_source": "Brent $97.29（2026-09-07，+1.05%，近七周高点）/ WTI $92.97 —— 布伦特上周涨 9.3%。周末美伊互袭油轮，德黑兰计划在霍尔木兹外设立海上禁区；高盛提示 $120 风险",
    # CNN 官方 Fear & Greed（0–100）作对照。页面主指标是自建代理指数，
    # 因为 CNN 的看跌看涨比率与广度分项 OpenD 取不到，无法自动化。
    "cnn_fear_greed": 41,
    "cnn_fear_greed_source_en": "CNN Fear & Greed 41 (Fear) on 2026-09-08 — down from 65 on 08-14, crossing out of Greed",
    "cnn_fear_greed_source": "CNN Fear & Greed 2026-09-08 读数 41（恐惧）—— 较 08-14 的 65 大幅回落，已跌出贪婪区",
    # 权重（合计 1.0）
    "weights": {"fed": 0.30, "earnings": 0.30, "news": 0.20, "technical": 0.20},
}

LEVELS = [
    (-100, -60, 1,
     ("Strongly Bearish", "极度看空"),
     ("Multiple headwinds converging — defense first, favor cash and hedges.",
      "多重利空共振，防御优先，现金与对冲为主。")),
    (-60, -20, 2,
     ("Moderately Bearish", "谨慎看空"),
     ("Downside pressure dominates — trim into rallies, cap exposure.",
      "下行压力占优，逢反弹减仓，控制敞口。")),
    (-20, 20, 3,
     ("Neutral", "中性"),
     ("Bulls and bears deadlocked — stay balanced, wait for a catalyst.",
      "多空拉锯、方向不明，均衡配置、等待催化。")),
    (20, 60, 4,
     ("Moderately Bullish", "谨慎看多"),
     ("Risk appetite firming — participate but size down, avoid chasing.",
      "风偏温和抬升，可参与但控仓，警惕追高。")),
    (60, 100, 5,
     ("Strongly Bullish", "强烈看多"),
     ("Uptrend well established — ride it, treat dips as opportunity.",
      "多头趋势明确，顺势加仓，回调即机会。")),
]


def to_level(score: float):
    for lo, hi, n, label, sub in LEVELS:
        if lo <= score < hi or (n == 5 and score >= 60):
            return n, F.bi(*label), F.bi(*sub)
    return 3, F.bi(*LEVELS[2][3]), F.bi(*LEVELS[2][4])


# 市值权重档位：(key, 英文, 中文) —— key 供前端上色，文案随语言切换
CAP_TIERS = {
    "vhigh": ("Mega", "极高"),
    "high": ("Large", "高"),
    "mid": ("Mid", "中"),
    "low": ("Small", "低"),
}


def cap_tier(mc: float | None) -> str:
    if not mc:
        return "low"
    if mc >= 1.5e12:
        return "vhigh"
    if mc >= 5e11:
        return "high"
    if mc >= 1e11:
        return "mid"
    return "low"


def build(as_of: str, dry_run: bool = False) -> dict:
    print(f"[1/5] 连接 OpenD 取数 (as_of={as_of}) …", file=sys.stderr)
    snap = F.fetch_all(as_of)
    if snap.errors:
        print("  取数告警:", file=sys.stderr)
        for e in snap.errors[:8]:
            print("   -", e, file=sys.stderr)
    print(
        f"  财报 {len(snap.earnings)} 条 / 经济事件 {len(snap.econ)} 条 / "
        f"新闻 {len(snap.news)} 条 / SPY K线 {len(snap.spy_klines)} 根",
        file=sys.stderr,
    )

    print("[2/5] 构建 Top100 口径 …", file=sys.stderr)
    universe = F.top_universe(snap.earnings, top_n=100)
    print(f"  去重后 {len(universe)} 家（按市值降序）", file=sys.stderr)

    print("[3/5] 计算四因子 …", file=sys.stderr)
    e_score, e_detail = F.earnings_score(universe)
    t_score, t_detail = F.technical_score(snap.spy_klines)
    macro, macro_hits = F.macro_surprise(snap.econ, as_of)
    f_score = F.fed_score(MANUAL["fed_policy_stance"], macro)
    curve = F.treasury_curve(snap.econ, as_of)
    if snap.ticker:
        print("  行情条:", "  ".join(
            f"{t['label']} {t['last']}({t['chg']:+.2f}%)" if t['chg'] is not None
            else f"{t['label']} {t['last']}" for t in snap.ticker), file=sys.stderr)
    if curve:
        print("  美债招标收益率:", "  ".join(
            f"{c['tenor']['zh']} {c['yield']:.3f}" for c in curve), file=sys.stderr)

    fg = F.fear_greed(snap.spy_klines, snap.tlt_klines,
                      snap.hyg_klines, snap.lqd_klines)
    if fg["score"] is not None:
        print(f"  恐慌贪婪代理 {fg['score']} ({fg['zone']['zh']}) "
              f"— {fg['componentCount']}/5 分项可用", file=sys.stderr)
        for c in fg["components"]:
            print(f"    {c['name']['zh']:<8} {c['value']:>5}  {c['detail']['zh']}",
                  file=sys.stderr)
        if fg["missing"]:
            print(f"    [!] 缺失分项: {', '.join(m['zh'] for m in fg['missing'])}",
                  file=sys.stderr)
    infl = F.inflation_tracker(snap.econ, as_of)
    if infl["latest"]:
        print("  通胀追踪:", file=sys.stderr)
        for r in infl["latest"]:
            print(
                f"    {r['label']['zh']:<14} {r['actual']}"
                f"  (预期 {r['consensus']}, 前值 {r['previous']})  {r['date']}",
                file=sys.stderr,
            )
    n_score, news_scored = F.news_score(snap.news)

    # 因子不可用时（例如 K 线配额耗尽导致技术面取不到）必须把它剔除并
    # 重新归一化其余权重 —— 直接按 0 计入会让"没数据"看起来像"中性判断"。
    w = MANUAL["weights"]
    raw = {"fed": f_score, "earnings": e_score, "news": n_score,
           "technical": t_score}
    avail = {k: v for k, v in raw.items() if v is not None}
    missing = [k for k, v in raw.items() if v is None]
    wsum = sum(w[k] for k in avail) or 1.0
    eff_w = {k: w[k] / wsum for k in avail}
    composite = round(sum(avail[k] * eff_w[k] for k in avail), 1)
    lvl_n, lvl_label, lvl_sub = to_level(composite)
    if missing:
        print(
            f"  [!] 因子不可用: {', '.join(missing)} —— 已剔除并按 "
            f"{ {k: round(v, 3) for k, v in eff_w.items()} } 重新归一化权重",
            file=sys.stderr,
        )

    print("[4/5] 计算风险等级 …", file=sys.stderr)
    vix = MANUAL["vix"]
    vix_is_proxy = vix is None
    if vix_is_proxy:
        vix = t_detail.get("rv21") or 20.0
    evt, evt_detail = F.event_density(snap.econ, universe, as_of)
    r_lvl, r_score = F.risk_level(vix, evt)

    def _f(v):
        return f"{v:+.1f}" if v is not None else "  n/a"

    print(
        f"  Fed {_f(f_score)} | 财报 {_f(e_score)} | 新闻 {_f(n_score)} | "
        f"技术 {_f(t_score)}  =>  综合 {composite:+.1f} → 等级 {lvl_n} {lvl_label['zh']}",
        file=sys.stderr,
    )
    print(f"  VIX {vix} + 事件密集度 {evt} => 风险等级 {r_lvl}", file=sys.stderr)

    # ---- 组装给前端的 DATA ----
    upcoming = _s_date(as_of)
    earn_rows = []
    for e in universe[:40]:
        d = str(e.get("date") or "")[:10]
        earn_rows.append(
            {
                "date": d,
                # 前端按语言渲染，这里只给 key
                "session": {"BEFORE": "pre", "AFTER": "post"}.get(
                    str(e.get("pub_type")), "intra"
                ),
                "tk": str(e.get("security") or "").replace("US.", ""),
                "co": e.get("name"),
                "est": e.get("eps_predict"),
                "act": e.get("eps_actual"),
                "kind": F.classify_surprise(
                    e.get("eps_actual"), e.get("eps_predict")
                ),
                "mcap": e.get("market_cap"),
                "w": cap_tier(e.get("market_cap")),
            }
        )
    earn_rows.sort(key=lambda r: (r["date"], -(r["mcap"] or 0)))

    # 宏观日程：未来事件优先（那才是风险来源），其次才是刚公布的。
    # as_of 就是今天时用真实当前时刻，否则今天早些时候已公布的事件
    # 会被误判成"待公布"。
    if as_of == dt.date.today().strftime("%Y-%m-%d"):
        now_ts = dt.datetime.now().timestamp()
    else:
        now_ts = dt.datetime.strptime(as_of, "%Y-%m-%d").timestamp() + 86400
    econ_all = []
    for ev in snap.econ:
        star = str(ev.get("star")).upper()
        if star not in ("HIGH", "MEDIUM"):
            continue
        ts = ev.get("ts") or 0
        econ_all.append(
            {
                "title": ev["title"],
                "date": dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                if ts
                else "",
                "ts": ts,
                "star": ev["star"],
                "actual": ev["raw_actual"],
                "consensus": ev["raw_consensus"],
                "upcoming": ts >= now_ts,
            }
        )
    upcoming = sorted(
        [e for e in econ_all if e["upcoming"]],
        key=lambda r: (r["star"] != "HIGH", r["ts"]),
    )
    past = sorted(
        [e for e in econ_all if not e["upcoming"]],
        key=lambda r: -r["ts"],
    )
    econ_rows = upcoming[:10] + past[:4]

    # 按关键词轮转取新闻，否则第一个关键词(Federal Reserve)会占满整个面板
    by_kw: dict[str, list] = {}
    for n in news_scored:
        by_kw.setdefault(n.get("keyword", "?"), []).append(n)
    interleaved, idx = [], 0
    while len(interleaved) < 12:
        added = False
        for kw in by_kw:
            if idx < len(by_kw[kw]):
                interleaved.append(by_kw[kw][idx])
                added = True
                if len(interleaved) >= 12:
                    break
        if not added:
            break
        idx += 1

    news_rows = [
        {
            "hl": n["title"][:130],
            "src": n.get("source"),
            "when": str(n.get("date") or "")[5:] or n.get("publish_time"),
            "url": n.get("url"),
            "senti": n["senti"],
            "impact": n.get("impact"),
            "risk": n.get("risk"),
            "kw": n.get("keyword"),
        }
        for n in interleaved
    ]

    spark = [k["close"] for k in snap.spy_klines[-60:]]

    data = {
        "asOf": as_of,
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "composite": composite,
        "level": {"n": lvl_n, "label": lvl_label, "sub": lvl_sub},
        "risk": {
            "level": r_lvl,
            "score": r_score,
            "vix": vix,
            "vixIsProxy": vix_is_proxy,
            "vixSource": F.bi(MANUAL["vix_source_en"], MANUAL["vix_source"])
            if not vix_is_proxy
            else F.bi("Proxy: SPY 21-day realized volatility",
                      "SPY 21日已实现波动率代理"),
            "eventDensity": evt,
            "eventDetail": evt_detail,
        },
        "weightsRenormalized": bool(missing),
        "missingFactors": missing,
        "klineFromCache": snap.kline_from_cache,
        "klineCacheStale": snap.kline_cache_stale,
        "drivers": [
            {
                "key": "fed",
                "name": F.bi("Fed Policy Rate", "Fed 利率"),
                "weight": eff_w.get("fed", w["fed"]),
                "baseWeight": w["fed"],
                "score": f_score,
                "auto": False,
                "detail": {
                    "stance": MANUAL["fed_policy_stance"],
                    "stanceReason": F.bi(MANUAL["fed_stance_reason_en"],
                                         MANUAL["fed_stance_reason"]),
                    "macroSurprise": macro,
                    "macroHits": macro_hits[:6],
                    "rateLow": MANUAL["fed_rate_low"],
                    "rateHigh": MANUAL["fed_rate_high"],
                    "nextFOMC": MANUAL["next_fomc"],
                    "source": F.bi(MANUAL["fed_source_en"],
                                   MANUAL["fed_source"]),
                },
            },
            {
                "key": "earnings",
                "name": F.bi("Top 100 Earnings", "Top100 财报"),
                "weight": eff_w.get("earnings", w["earnings"]),
                "baseWeight": w["earnings"],
                "score": e_score,
                "auto": True,
                "detail": e_detail,
            },
            {
                "key": "news",
                "name": F.bi("Major News", "重大新闻"),
                "weight": eff_w.get("news", w["news"]),
                "baseWeight": w["news"],
                "score": n_score,
                "auto": True,
                "detail": {"count": len(news_scored), "method": "关键词启发式"},
            },
            {
                "key": "technical",
                "name": F.bi("Technical Sentiment", "技术面情绪"),
                "weight": eff_w.get("technical", w["technical"]),
                "baseWeight": w["technical"],
                "score": t_score,
                "auto": True,
                "detail": t_detail,
            },
        ],
        "ticker": snap.ticker,
        "treasuryCurve": curve,
        "oilSpot": {
            "brent": MANUAL.get("brent_spot"),
            "wti": MANUAL.get("wti_spot"),
            "source": F.bi(MANUAL.get("oil_spot_source_en", ""),
                           MANUAL.get("oil_spot_source", "")),
        },
        "fearGreed": {
            **fg,
            # CNN 官方值作对照 —— 人工维护，代理指数与它不会完全一致
            "cnnReference": MANUAL.get("cnn_fear_greed"),
            "cnnSource": F.bi(MANUAL.get("cnn_fear_greed_source_en", ""),
                              MANUAL.get("cnn_fear_greed_source", "")),
        },
        "inflation": infl,
        "earnings": earn_rows,
        "econ": econ_rows[:14],
        "news": news_rows,
        "spark": spark,
        "errors": snap.errors[:5],
    }

    if dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        return data

    print("[5/5] 渲染 HTML …", file=sys.stderr)
    render(data)
    print(f"  已写出 {OUTPUT}", file=sys.stderr)
    return data


def _s_date(s: str) -> str:
    return s


def render(data: dict) -> None:
    if not TEMPLATE.exists():
        raise SystemExit(f"模板缺失: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    start, end = "/*DATA_START*/", "/*DATA_END*/"
    i, j = html.find(start), html.find(end)
    if i == -1 or j == -1:
        raise SystemExit("模板里找不到 DATA_START / DATA_END 标记")
    html = html[: i + len(start)] + "\nconst DATA = " + payload + ";\n" + html[j:]
    OUTPUT.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    build(a.date, a.dry_run)
