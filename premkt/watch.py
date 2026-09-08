"""
全市场实时暴涨监控 —— 每 N 秒扫一次，命中就弹 macOS 通知。

为什么可以扫全市场：batch=400 时 7,161 只快照实测只要 3.8 秒（18 次调用），
30 秒周期里只用掉 OpenD 限频（60次/30秒）的 30%。所以不需要预筛 universe，
不会漏掉"平时没成交、突然拉起来"的票 —— 那恰恰是最该抓的一类。

"暴涨"的定义（三个条件同时成立，缺一不可）:
    1. 窗口涨幅   —— 近 N 分钟涨幅 >= surge_pct
    2. 放量确认   —— 窗口内每分钟成交量 / 今日均速 >= vol_surge
    3. 可交易     —— 价格、今日成交额达标
只看价格会抓到一堆无量的仙股跳价；只看量会抓到平价大宗。必须同时成立。

用法:
    python -m premkt.watch                          # 默认 30 秒一轮
    python -m premkt.watch --interval 15 --surge 3  # 15 秒一轮，3% 触发
    python -m premkt.watch --window 5               # 用 5 分钟窗口
    python -m premkt.watch --down                   # 同时监控暴跌
    python -m premkt.watch --dry-run                # 不弹通知，只打印
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections import deque


from .data import HERE, now_et, quote_ctx, session_label, snapshots
from .fmt import c as _c, lj, rj, trunc
from .intraday import full_universe

CFG = dict(
    interval=30.0,        # 扫描间隔（秒）
    window_min=2.0,       # 涨幅观察窗口（分钟）
    surge_pct=2.0,        # 窗口涨幅触发线 %
    vol_surge=2.0,        # 窗口成交速度 / 今日均速
    min_price=1.0,
    min_turnover=2_000_000.0,   # 今日累计成交额下限
    cooldown_min=10.0,    # 同一只票的冷却时间（分钟）
    re_alert_pct=2.0,     # 冷却期内再涨这么多则允许再报
    max_notify=4,         # 单轮最多弹几条通知，其余汇总进终端
    session_ok=("premarket", "regular", "afterhours"),
)

ALERT_LOG = os.path.join(HERE, "alerts.csv")
CTL_FILE = os.path.join(HERE, "watch.ctl")

# 可在运行中热改的键（其余键忽略，防止手滑写坏）
LIVE_KEYS = {"paused", "mute", "down", "interval", "window_min", "surge_pct",
             "vol_surge", "min_turnover", "min_price", "cooldown_min", "max_notify"}


def read_ctl() -> dict:
    """读控制文件。文件不存在 / 内容坏了都不该让监控挂掉。"""
    try:
        with open(CTL_FILE) as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if k in LIVE_KEYS}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(_c(f"  [ctl] 解析失败，忽略本次改动: {exc}", "yel"))
        return {}


def write_ctl(d: dict) -> None:
    cur = read_ctl()
    cur.update({k: v for k, v in d.items() if k in LIVE_KEYS})
    with open(CTL_FILE, "w") as f:
        json.dump(cur, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
def notify(title: str, subtitle: str, body: str, sound: str = "Ping") -> None:
    """macOS 右上角通知。osascript 对引号敏感，先做转义。"""
    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{esc(body)}" with title "{esc(title)}" '
              f'subtitle "{esc(subtitle)}" sound name "{esc(sound)}"')
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True, timeout=5)
    except Exception as exc:
        print(_c(f"  [notify] 失败: {exc}", "red"))


# ---------------------------------------------------------------------------
class Tracker:
    """每只票的滚动历史。

    缓冲按 MAX_WINDOW_MIN 预留（不是按当前 window），这样运行中调 window
    或 interval 都不会丢历史、不必重新预热。
    """
    MAX_WINDOW_MIN = 15.0

    def __init__(self, window_min: float, interval: float):
        self.interval = interval
        self.need = max(2, int(math.ceil(window_min * 60 / interval)) + 1)
        self.cap = max(self.need, int(math.ceil(self.MAX_WINDOW_MIN * 60 / interval)) + 1)
        self.hist: dict[str, deque] = {}
        self.last_alert: dict[str, tuple[dt.datetime, float]] = {}

    def push(self, code: str, ts: float, price: float, volume: float) -> None:
        d = self.hist.setdefault(code, deque(maxlen=self.cap))
        d.append((ts, price, volume))

    def retune(self, window_min: float, interval: float) -> None:
        """window / interval 变了只需重算 need，缓冲长度不动。"""
        self.interval = interval
        self.need = max(2, int(math.ceil(window_min * 60 / interval)) + 1)

    def measure(self, code: str, window_sec: float):
        """返回 (窗口涨幅%, 窗口内每分钟成交量, 窗口实际跨度秒) 或 None。"""
        d = self.hist.get(code)
        if not d or len(d) < 2:
            return None
        now_ts, now_px, now_vol = d[-1]
        ref = None
        for ts, px, vol in d:                      # 取最早一个仍在窗口内的点
            if now_ts - ts <= window_sec * 1.5:
                ref = (ts, px, vol); break
        if ref is None or ref[0] == now_ts:
            return None
        span = now_ts - ref[0]
        if span < window_sec * 0.4:                # 样本还不够长，别急着判
            return None
        if ref[1] <= 0:
            return None
        ret = (now_px / ref[1] - 1) * 100
        vpm = (now_vol - ref[2]) / (span / 60.0)
        return ret, vpm, span


# ---------------------------------------------------------------------------
def _minutes_into_session(ts: dt.datetime) -> float:
    """今日已交易分钟数，用来估算成交量均速。"""
    sess = session_label(ts)
    if sess == "regular":
        start = ts.replace(hour=9, minute=30, second=0, microsecond=0)
    elif sess == "premarket":
        start = ts.replace(hour=4, minute=0, second=0, microsecond=0)
    elif sess == "afterhours":
        start = ts.replace(hour=9, minute=30, second=0, microsecond=0)
    else:
        return float("nan")
    return max((ts - start).total_seconds() / 60.0, 1.0)


def scan_once(q, universe: list[str], tr: Tracker, args) -> list[dict]:
    now = now_et()
    snap = snapshots(q, universe, quiet=True, batch=400)
    if snap.empty:
        return []
    ts = time.time()
    mins = _minutes_into_session(now)
    window_sec = CFG["window_min"] * 60
    hits = []

    for r in snap.itertuples():
        px = float(getattr(r, "last_price", 0) or 0)
        vol = float(getattr(r, "volume", 0) or 0)
        if px <= 0:
            continue
        tr.push(r.code, ts, px, vol)

        if px < CFG["min_price"]:
            continue
        to = float(getattr(r, "turnover", 0) or 0)
        if to < CFG["min_turnover"]:
            continue

        m = tr.measure(r.code, window_sec)
        if m is None:
            continue
        ret, vpm, span = m
        if not (ret >= CFG["surge_pct"] or (args.down and ret <= -CFG["surge_pct"])):
            continue

        # 放量确认：窗口成交速度 vs 今日均速
        base = vol / mins if (mins == mins and mins > 0 and vol > 0) else float("nan")
        surge = vpm / base if (base == base and base > 0) else float("nan")
        if surge == surge and surge < CFG["vol_surge"]:
            continue

        prev = float(getattr(r, "prev_close_price", 0) or 0)
        day = (px / prev - 1) * 100 if prev > 0 else float("nan")

        # 冷却：除非又走了 re_alert_pct
        la = tr.last_alert.get(r.code)
        if la:
            gap = (now - la[0]).total_seconds() / 60.0
            if gap < CFG["cooldown_min"] and abs(px / la[1] - 1) * 100 < CFG["re_alert_pct"]:
                continue

        tr.last_alert[r.code] = (now, px)
        hits.append(dict(
            ts=now, code=r.code, name=str(getattr(r, "name", "")),
            price=px, win_ret=ret, span=span, day_ret=day,
            vol_surge=surge, turnover=to,
            high=float(getattr(r, "high_price", 0) or 0),
            vwap=float(getattr(r, "avg_price", 0) or 0),
        ))
    return sorted(hits, key=lambda h: -abs(h["win_ret"]))


# ---------------------------------------------------------------------------
def log_alerts(hits: list[dict]) -> None:
    new = not os.path.exists(ALERT_LOG)
    with open(ALERT_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_et", "code", "name", "price", "win_ret_pct",
                        "span_sec", "day_ret_pct", "vol_surge", "turnover"])
        for h in hits:
            w.writerow([h["ts"].strftime("%Y-%m-%d %H:%M:%S"), h["code"], h["name"],
                        f"{h['price']:.4f}", f"{h['win_ret']:.2f}", f"{h['span']:.0f}",
                        f"{h['day_ret']:.2f}", f"{h['vol_surge']:.2f}", f"{h['turnover']:.0f}"])


def print_hits(hits: list[dict]) -> None:
    for h in hits:
        col = "grn" if h["win_ret"] > 0 else "red"
        vs = "n/a" if h["vol_surge"] != h["vol_surge"] else f"{h['vol_surge']:.1f}x"
        near_hi = "  贴日高" if h["high"] > 0 and h["price"] >= h["high"] * 0.999 else ""
        print("  " + _c("▲" if h["win_ret"] > 0 else "▼", col)
              + lj(f" {h['code'][3:]}", 8) + lj(trunc(h["name"], 16), 18)
              + rj(f"{h['price']:.2f}", 9)
              + rj(_c(f"{h['win_ret']:+.2f}%", col), 9) + _c(f"/{h['span']:.0f}s", "dim")
              + rj(f"今日{h['day_ret']:+.1f}%", 12)
              + rj(f"量{vs}", 8)
              + rj(f"额${h['turnover']/1e6:.0f}M", 10) + _c(near_hi, "yel"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="全市场实时暴涨监控")
    p.add_argument("--interval", type=float, default=CFG["interval"], help="扫描间隔秒数")
    p.add_argument("--window", type=float, default=CFG["window_min"], help="涨幅窗口（分钟）")
    p.add_argument("--surge", type=float, default=CFG["surge_pct"], help="窗口涨幅触发线 %%")
    p.add_argument("--vol-surge", type=float, default=CFG["vol_surge"], help="放量倍数下限")
    p.add_argument("--min-turnover", type=float, default=CFG["min_turnover"], help="今日成交额下限")
    p.add_argument("--min-price", type=float, default=CFG["min_price"])
    p.add_argument("--cooldown", type=float, default=CFG["cooldown_min"], help="同票冷却分钟")
    p.add_argument("--down", action="store_true", help="同时监控暴跌")
    p.add_argument("--dry-run", action="store_true", help="不弹通知，只打印")
    p.add_argument("--any-session", action="store_true", help="休市时段也扫（调试用）")
    a = p.parse_args(argv)
    CFG.update(interval=a.interval, window_min=a.window, surge_pct=a.surge,
               vol_surge=a.vol_surge, min_turnover=a.min_turnover,
               min_price=a.min_price, cooldown_min=a.cooldown)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    print(_c(f"\n全市场暴涨监控  {now_et():%Y-%m-%d %H:%M:%S ET}", "bold"))
    print(_c(f"  触发条件: 近 {CFG['window_min']:.0f} 分钟涨幅 ≥ {CFG['surge_pct']}% "
             f"且 放量 ≥ {CFG['vol_surge']}x 今日均速 "
             f"且 今日成交额 ≥ ${CFG['min_turnover']/1e6:.0f}M", "dim"))
    print(_c(f"  扫描间隔 {CFG['interval']:.0f}s  冷却 {CFG['cooldown_min']:.0f}min  "
             f"{'含暴跌' if a.down else '仅暴涨'}  "
             f"{'[dry-run 不弹通知]' if a.dry_run else '通知: macOS 横幅'}", "dim"))
    print(_c(f"  告警记录: {ALERT_LOG}    Ctrl-C 退出\n", "dim"))

    tr = Tracker(CFG["window_min"], CFG["interval"])
    universe, uni_at = [], 0.0
    cycle = 0
    ctl_mtime, paused, muted = 0.0, False, False

    with quote_ctx() as q:
        while not stop["flag"]:
            t0 = time.time()
            now = now_et()

            # --- 热加载控制文件（只在 mtime 变化时读，省 IO）---
            try:
                m = os.path.getmtime(CTL_FILE)
            except OSError:
                m = 0.0
            if m != ctl_mtime:
                ctl_mtime = m
                ctl = read_ctl()
                changed = {k: v for k, v in ctl.items()
                           if k not in ("paused", "mute") and CFG.get(k) != v}
                CFG.update({k: v for k, v in ctl.items() if k not in ("paused", "mute")})
                new_paused, new_muted = bool(ctl.get("paused")), bool(ctl.get("mute"))
                if changed:
                    tr.retune(CFG["window_min"], CFG["interval"])
                    print("\n" + _c(f"  [ctl] 参数已更新: {changed}", "cyn"))
                if new_paused != paused:
                    print("\n" + _c("  [ctl] 已暂停（扫描停止，历史保留）" if new_paused
                                    else "  [ctl] 已恢复", "yel" if new_paused else "grn"))
                if new_muted != muted:
                    print("\n" + _c(f"  [ctl] 通知{'已静音（仍记录到 CSV）' if new_muted else '已恢复'}", "cyn"))
                paused, muted = new_paused, new_muted

            if paused:
                print(_c(f"\r  {now:%H:%M:%S} 已暂停 —— "
                         f"python -m premkt.watchctl resume 恢复" + " " * 20, "yel"),
                      end="", flush=True)
                time.sleep(min(CFG["interval"], 5))
                continue

            sess = session_label(now)
            if sess not in CFG["session_ok"] and not a.any_session:
                print(_c(f"\r  {now:%H:%M:%S} 非交易时段({sess})，等待中…", "dim"), end="", flush=True)
                time.sleep(min(CFG["interval"], 30))
                continue

            if not universe or time.time() - uni_at > 1800:   # 半小时刷新一次名单
                universe = full_universe(q)["code"].tolist()
                uni_at = time.time()
                print(_c(f"  [universe] {len(universe)} 只普通股", "dim"))

            try:
                hits = scan_once(q, universe, tr, a)
            except Exception as exc:
                print(_c(f"  [scan] 异常: {exc}", "red"))
                time.sleep(CFG["interval"]); continue

            cycle += 1
            el = time.time() - t0
            warm = "" if len(tr.hist) and cycle > tr.need else _c(f" (预热 {cycle}/{tr.need})", "yel")
            print(f"\r  {now:%H:%M:%S} 第{cycle}轮 {len(universe)}只 {el:.1f}s "
                  f"命中 {len(hits)}{warm}" + " " * 20, end="", flush=True)

            if hits:
                print()
                print_hits(hits)
                log_alerts(hits)
                if not a.dry_run and not muted:
                    for h in hits[:CFG["max_notify"]]:
                        arrow = "暴涨" if h["win_ret"] > 0 else "暴跌"
                        notify(f"{arrow} {h['code'][3:]}  {h['win_ret']:+.2f}%",
                               f"{h['price']:.2f}  今日{h['day_ret']:+.1f}%  "
                               f"量{h['vol_surge']:.1f}x",
                               f"{trunc(h['name'], 28)} · 近{h['span']:.0f}秒 · "
                               f"成交额${h['turnover']/1e6:.0f}M",
                               sound="Ping" if h["win_ret"] > 0 else "Basso")
                    if len(hits) > CFG["max_notify"]:
                        rest = ", ".join(x["code"][3:] for x in hits[CFG["max_notify"]:][:8])
                        notify(f"另有 {len(hits)-CFG['max_notify']} 只同时异动",
                               "已写入 alerts.csv", rest, sound="Pop")

            time.sleep(max(0.0, CFG["interval"] - (time.time() - t0)))

    print(_c(f"\n\n已停止。共 {cycle} 轮，告警见 {ALERT_LOG}", "bold"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
