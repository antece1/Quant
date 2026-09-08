"""
数据层 —— 全部来自本地 moomoo/Futu OpenD (127.0.0.1:11111)。

已实测确认的接口行为（futu 10.09.6908）:
  get_us_pre_market_rank(sort_dir, count<=200, offset, filter_list)
      -> (ret, (all_count, DataFrame))   注意是嵌套元组
      列: security/name/pre_market_price/pre_market_change_ratio/
          pre_market_change_amount/pre_market_turnover/pre_market_volume/
          close_price/change_ratio/change_amount
      close_price 是"上一个交易日收盘价"所在那天的收盘 —— 盘前时段它就是昨收，
      收盘后它是今收，因此本模块只在盘前时段把它当昨收用。
  get_market_snapshot(codes)  -> 单次最多 400 只，含完整盘前 OHLCV + 股本 + 融券
  get_stock_basicinfo(Market.US, SecurityType.STOCK) -> 13k 只普通股，自带
      exchange_type，用它剔除 ETF / 权证 / 粉单
  request_history_kline(...) -> 3 元组 (ret, df, page_key)，日线可回溯数年

OpenD 频率限制：快照与历史 K 线均为 30 秒内 60 次，本模块用令牌桶节流。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import pandas as pd
from futu import (
    RET_OK,
    AuType,
    KLType,
    Market,
    OpenQuoteContext,
    RankSortDir,
    SecurityType,
    SubType,
)

from .config import RUNTIME

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")


# ---------------------------------------------------------------------------
# 节流：30 秒滑动窗口内最多 N 次调用
# ---------------------------------------------------------------------------
class _Throttle:
    def __init__(self, max_calls: int = 55, window: float = 30.0):
        self.max_calls, self.window = max_calls, window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            while self._calls and now - self._calls[0] > self.window:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.window - (now - self._calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
                now = time.time()
                while self._calls and now - self._calls[0] > self.window:
                    self._calls.popleft()
            self._calls.append(time.time())


_throttle = _Throttle()


@contextmanager
def quote_ctx():
    q = OpenQuoteContext(host=RUNTIME["host"], port=RUNTIME["port"])
    try:
        yield q
    finally:
        q.close()


# ---------------------------------------------------------------------------
# 时段判断
# ---------------------------------------------------------------------------
def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def session_label(ts: dt.datetime | None = None) -> str:
    """premarket / regular / afterhours / overnight / closed。

    实测重点：pre_* 字段与盘前榜在美东 04:00 之前不会翻到新的一天。
    也就是说 02:00 ET 跑扫描，拿到的是"上一个交易日"的盘前数据，而且
    update_time 照常刷新、字段完全正常 —— 这是最危险的一类脏数据。
    overnight_* 字段则是实时的（Blue Ocean ATS，20:00–04:00 ET）。
    """
    ts = ts or now_et()
    t = ts.time()
    wd = ts.weekday()
    if wd < 5 and dt.time(4, 0) <= t < dt.time(9, 30):
        return "premarket"
    if wd < 5 and dt.time(9, 30) <= t < dt.time(16, 0):
        return "regular"
    if wd < 5 and dt.time(16, 0) <= t < dt.time(20, 0):
        return "afterhours"
    # 夜盘（Blue Ocean ATS）：周日 20:00 ET ~ 周五 04:00 ET
    if t >= dt.time(20, 0) and wd in (6, 0, 1, 2, 3):
        return "overnight"
    if t < dt.time(4, 0) and wd in (0, 1, 2, 3, 4):
        return "overnight"
    return "closed"


def premarket_data_is_current(ts: dt.datetime | None = None) -> bool:
    """pre_* 字段是否属于"今天"。04:00 ET 之前一律不是。"""
    return session_label(ts) in ("premarket", "regular", "afterhours")


# ---------------------------------------------------------------------------
# 盘前榜
# ---------------------------------------------------------------------------
def pre_market_rank(q, n: int, gainers: bool = True) -> pd.DataFrame:
    """分页拉取盘前涨/跌幅榜。单次上限 200，用 offset 翻页。"""
    sort_dir = RankSortDir.DESCENDING if gainers else RankSortDir.ASCENDING
    frames, offset = [], 0
    while offset < n:
        want = min(200, n - offset)
        _throttle.wait()
        ret, data = q.get_us_pre_market_rank(sort_dir=sort_dir, count=want, offset=offset)
        if ret != RET_OK:
            raise RuntimeError(f"get_us_pre_market_rank failed: {data}")
        all_count, df = data
        if df is None or df.empty:
            break
        frames.append(df)
        offset += len(df)
        if offset >= all_count:
            break
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset="security")
    out["side"] = "long" if gainers else "short"
    return out


# ---------------------------------------------------------------------------
# 普通股名单（剔除 ETF / 权证 / 粉单）
# ---------------------------------------------------------------------------
def us_common_stocks(q, use_cache: bool = True) -> pd.DataFrame:
    """13k 只 US STOCK 类证券。按天缓存，避免每次跑都拉一遍。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"us_stocks_{now_et():%Y-%m-%d}.parquet")
    if use_cache and os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception:
            pass
    _throttle.wait()
    ret, df = q.get_stock_basicinfo(Market.US, SecurityType.STOCK)
    if ret != RET_OK:
        raise RuntimeError(f"get_stock_basicinfo failed: {df}")
    keep = ["code", "name", "exchange_type", "listing_date", "delisting", "stock_type"]
    df = df[[c for c in keep if c in df.columns]].copy()
    try:
        df.to_parquet(path, index=False)
    except Exception:
        pass  # parquet 引擎缺失不影响主流程
    return df


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
SNAPSHOT_FIELDS = [
    "code", "name", "update_time", "last_price", "open_price", "prev_close_price",
    "high_price", "low_price", "volume", "turnover", "suspension", "sec_status",
    "listing_date", "lot_size", "ask_price", "bid_price", "ask_vol", "bid_vol",
    "enable_short_sell", "short_available_volume", "amplitude", "volume_ratio",
    # 盘中需要：avg_price 是当日 VWAP，快照没有 change_rate，涨幅要自己算
    "avg_price", "bid_ask_ratio", "turnover_rate", "price_spread",
    # 期权合约快照用（options.py 判断 0DTE 流动性）
    "option_open_interest", "option_implied_volatility", "option_delta", "option_gamma",
    "option_strike_price", "strike_time", "option_type",
    "highest52weeks_price", "lowest52weeks_price",
    "highest_history_price", "lowest_history_price",
    "issued_shares", "outstanding_shares", "total_market_val", "circular_market_val",
    "pe_ttm_ratio", "earning_per_share",
    "pre_price", "pre_high_price", "pre_low_price", "pre_volume", "pre_turnover",
    "pre_change_val", "pre_change_rate", "pre_amplitude",
]


def _snapshot_chunk(q, chunk: list[str], skipped: list[str]) -> list[pd.DataFrame]:
    """取一批快照；失败就二分拆批，把真正有问题的代码单独剔除。

    单个代码没有行情权限（例如 "US OTC market quote is not available for STRS"）
    会让整批 400 只一起失败。基础信息里的 exchange_type 并不总能提前识别这类票，
    所以只能靠拆批定位。
    """
    _throttle.wait()
    ret, df = q.get_market_snapshot(chunk)
    if ret == RET_OK:
        return [df]
    if len(chunk) == 1:
        # 单只失败先重试一次：整批连续代码同时"无权限"通常是瞬时错误，
        # 直接跳过会悄悄丢掉 OTIS 这种正常的大盘股
        time.sleep(0.3)
        _throttle.wait()
        ret2, df2 = q.get_market_snapshot(chunk)
        if ret2 == RET_OK:
            return [df2]
        skipped.append(chunk[0])
        return []
    mid = len(chunk) // 2
    return (_snapshot_chunk(q, chunk[:mid], skipped)
            + _snapshot_chunk(q, chunk[mid:], skipped))


def snapshots(q, codes: list[str], quiet: bool = False,
              batch: int | None = None) -> pd.DataFrame:
    """分批取快照。单次上限 400。

    batch=400 时全市场 7161 只约 3.8s（18 次调用），batch=200 约 8.0s（36 次）——
    实时监控要用 400，离线分析用默认值即可。
    """
    batch = batch or RUNTIME["snapshot_batch"]
    frames, skipped = [], []
    for i in range(0, len(codes), batch):
        frames += _snapshot_chunk(q, codes[i : i + batch], skipped)
    if skipped and not quiet:
        head = ", ".join(s.split(".")[-1] for s in skipped[:6])
        print(f"      跳过 {len(skipped)} 只无行情权限的代码: {head}"
              f"{' …' if len(skipped) > 6 else ''}")
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out[[c for c in SNAPSHOT_FIELDS if c in out.columns]]


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------
KLINE_CACHE = os.path.join(CACHE_DIR, "klines")

# 实测：OpenD 的历史 K 线配额是"7 天窗口内最多 100 只不同股票"，
# 报错原文 "Insufficient historical K-line quota (stock: 100/100)"。
# 每天跑一次扫描（50 只/次）两天就打满，所以必须落地缓存 —— 同一只票
# 当天重复取一律走本地，别去消耗额度。
_quota_exhausted = [False]


def _kline_cache_path(code: str, ktype: str) -> str:
    return os.path.join(KLINE_CACHE, f"{code.replace('.', '_')}_{ktype}.csv")


def cached_daily_kline(q, code: str, start: dt.date, end: dt.date,
                       max_age_hours: float = 12.0) -> pd.DataFrame | None:
    """带磁盘缓存的日线。缓存足够新且覆盖所需区间时不发请求。"""
    os.makedirs(KLINE_CACHE, exist_ok=True)
    path = _kline_cache_path(code, "D")
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        try:
            cached = pd.read_csv(path)
            if len(cached):
                first = pd.to_datetime(cached["time_key"]).dt.date.min()
                if age_h < max_age_hours and first <= start:
                    return cached
        except Exception:
            cached = None

    if _quota_exhausted[0]:
        df = sub_daily_kline(q, code)
        if df is not None and not df.empty:
            try:
                df.to_csv(path, index=False)
            except Exception:
                pass
            return df
        return _stale_cache(path)

    _throttle.wait()
    try:
        ret, df, _ = q.request_history_kline(
            code, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
            ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=1000)
    except Exception as exc:
        print(f"  [kline] {code} 异常: {exc}")
        return _stale_cache(path)

    if ret != RET_OK:
        msg = str(df)
        if "quota" in msg.lower() or "额度" in msg:
            if not _quota_exhausted[0]:
                print("  [kline] 历史 K 线配额已用尽 —— 改走订阅通道 get_cur_kline")
            _quota_exhausted[0] = True
        df = None

    if df is None or df.empty:
        df = sub_daily_kline(q, code)      # 订阅通道不占历史额度
        if df is None or df.empty:
            return _stale_cache(path)

    try:
        df.to_csv(path, index=False)
    except Exception:
        pass
    return df


def _stale_cache(path: str) -> pd.DataFrame | None:
    """配额耗尽时退回过期缓存，聊胜于无（调用方自己判断新鲜度）。"""
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            return None
    return None


def sub_daily_kline(q, code: str, num: int = 400) -> pd.DataFrame | None:
    """走订阅通道取日线 —— 关键：它不消耗 request_history_kline 的历史额度。

    历史额度是"7 天窗口内 100 只不同股票"，扫描器很容易打满。
    get_cur_kline 最多给 1000 根，实测 400 根日线可回溯约 1.5 年，
    对财报反应统计足够。用完立刻退订，避免占用订阅额度。

    注意它没有 change_rate 列，这里用 last_close 补上，保持与
    request_history_kline 的列一致。
    """
    ret, _ = q.subscribe([code], [SubType.K_DAY])
    if ret != RET_OK:
        return None
    try:
        for _ in range(3):
            ret2, k = q.get_cur_kline(code, num, KLType.K_DAY, AuType.QFQ)
            if ret2 == RET_OK and k is not None and len(k):
                k = k.copy()
                if "change_rate" not in k.columns:
                    lc = pd.to_numeric(k["last_close"], errors="coerce")
                    cl = pd.to_numeric(k["close"], errors="coerce")
                    k["change_rate"] = (cl / lc - 1) * 100
                return k
            time.sleep(0.4)          # 订阅刚建立时数据可能还没推过来
    finally:
        try:
            q.unsubscribe([code], [SubType.K_DAY])
        except Exception:
            pass
    return None


def daily_klines(q, codes: list[str], days: int | None = None) -> dict[str, pd.DataFrame]:
    """逐只拉日线，优先走磁盘缓存以节省历史行情配额。"""
    days = days or RUNTIME["kline_days"]
    end = now_et().date()
    start = end - dt.timedelta(days=int(days * 1.6))  # 日历日 -> 交易日留余量
    out, missed = {}, 0
    for code in codes:
        df = cached_daily_kline(q, code, start, end)
        if df is not None and not df.empty:
            out[code] = df
        else:
            missed += 1
    if missed:
        print(f"  [kline] {missed}/{len(codes)} 只无数据（配额或新股）")
    return out


# ---------------------------------------------------------------------------
# 快照落盘（用于事后前向验证）
# ---------------------------------------------------------------------------
def save_snapshot(payload: dict, tag: str = "") -> str:
    d = os.path.join(HERE, "snapshots")
    os.makedirs(d, exist_ok=True)
    stamp = now_et().strftime("%Y-%m-%d_%H%M")
    path = os.path.join(d, f"scan_{stamp}{('_' + tag) if tag else ''}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return path
