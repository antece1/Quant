# Options — 期权分析工具箱

moomoo/Futu OpenD 实盘期权数据 + 策略引擎。所有脚本用仓库自带虚拟环境运行：

```
/Users/antaiwei/Claude\ Code/.venv/bin/python
```

**前置条件：moomoo OpenD 必须在 127.0.0.1:11111 运行**（拉数据的脚本才能工作；纯分析脚本读本地 JSON 快照，不需要）。

---

## 结构

```
Options/
├── optlib.py          共享路径解析。导入它就把 Options/ 和 tools/ 挂上 sys.path，
│                      所以任何子目录里的脚本都能互相 import、并读到任意快照。
├── tools/             通用工具（不绑定标的）
├── NBIS/ COHR/        按标的：分析脚本 + 它自己的链快照放一起
├── studies/           一次性研究
├── backtest/          optbt 引擎 + 运行器
└── positions/         持仓快照、Webull 记录、组合分析器
```

## tools/ — 通用工具

| 脚本 | 用途 |
|---|---|
| `leaps_chain_pull.py TICKER` | 拉全期限期权链 + 5年日线 → 写入 `Options/TICKER/` |
| `leaps_selector.py TICKER` | LEAPS 选择：租金/年、杠杆、破本波动率、$每单位delta |
| `gex_profile.py TICKER [EXP]` | 做市商 gamma 敞口分布、墙的位置、到期衰减 |
| `moomoo_option_price.py` | 单合约报价/K线；Futu 合约代码构造 |
| `moomoo_fills.py` | 拉 moomoo 成交记录 |
| `alphavantage_options.py` | 历史期权链（moomoo 只有约5天期权历史时的备选） |
| `backtest_position.py` | 单笔持仓回测 |

不指定日期时，`gex_profile` 和 `leaps_selector` **自动使用该标的最新的快照**；指定用 `ASOF=2026-08-20`。

```bash
V="/Users/antaiwei/Claude Code/.venv/bin/python"; O="/Users/antaiwei/Claude Code/Options"
"$V" "$O/tools/leaps_chain_pull.py" NBIS          # 拉最新链
"$V" "$O/tools/gex_profile.py" NBIS               # 用最新快照
ASOF=2026-08-14 "$V" "$O/tools/gex_profile.py" NBIS   # 指定历史快照
"$V" "$O/optlib.py"                                # 列出所有已有快照
```

## 踩过的坑（别再踩）

1. **限频**：`get_option_chain` 每 30 秒最多 10 次。拉长期限标的（14+ 到期）必然触发，`leaps_chain_pull.py` 已内置重试+节流。
2. **`N/A` 字符串**：moomoo 对没有数据的字段返回字符串 `'N/A'`，不是 `None`。直接 `float()` 会炸，用脚本里的 `num()`。
3. **返回值元组长度不一**：`get_market_snapshot` 返回 2 个值，`request_history_kline` 返回 3 个。用 `call2()` 统一。
4. **期权历史只有约 5 天**：正股日线有几年，期权 K 线没有。深度历史入场价只能靠成交记录或 Alpha Vantage。
5. **GEX 的预测寿命约一天**。gamma 是 moneyness 的函数——股价一动 20%，每个行权价的权重全部重排，"墙"不是被突破而是不再是墙。2026-08-20 已实证：8/14 测到的 250 支撑，到 8/18 完全失效。
6. **theta × 剩余天数 = 错**。theta 是一阶导数不是速率，会加速。周期权最后一天烧掉的比前三天加起来还多，用 BS 逐日重估（见 `COHR/cohr_theta_race.py`）。
7. **历史 EV 会继承样本漂移**。对数收益去均值后仍残留 `exp(σ²T/2)` 的算术漂移（525 天 @63% vol ＝ +33%），必须把模拟路径归一化到 `E[S_T]=S`，否则长期限期权会被系统性高估。
8. **P(收在X上) ≠ P(触及X)**，零漂移下差约 2 倍。打算冲高就卖的，看触及概率。

## 数据约定

链快照文件名 `{ticker}_chain_YYYY-MM-DD.json`，放在该标的自己的文件夹里。
`optlib.latest_chain()` 靠文件名排序取最新，所以**不要重命名**。
