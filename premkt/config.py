"""
盘前动量选股 — 全部可调参数集中在这里。

设计原则：硬门槛(GATES)负责剔除不可交易的东西，权重(WEIGHTS)负责排序，
催化剂(CATALYST_MULT)是乘数而不是加分项 —— 这是本策略和普通 gap scanner
最重要的区别。低流通盘 + 无催化剂 = 纯逼空，必须被系统性压低，而不是
靠一个加分项去和其他维度平均掉。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 硬门槛：不满足直接剔除，不进入打分
# ---------------------------------------------------------------------------
GATES = dict(
    min_price=2.0,            # 低于 $2 的票点差和操纵风险不成比例
    max_price=500.0,
    min_gap_pct=2.0,          # 盘前涨幅下限（做空侧取绝对值）
    max_gap_pct=300.0,        # 极端 gap 多为仙股逼空，另行处理
    min_pre_turnover=1_000_000.0,   # 盘前成交额 ≥ $1M —— 最有效的单一过滤器
    min_pre_volume=50_000,          # 盘前成交量下限
    min_market_cap=50_000_000.0,    # 市值 ≥ $5000 万
    max_spread_pct=2.0,       # 盘前买卖价差 / 中间价，超过则不可交易
    min_days_listed=20,       # 次新股（上市 <20 天）行为模式不同，默认排除
    # 只要主板；US_PINK / OTC 的盘前报价不可信
    allowed_exchanges={"US_NASDAQ", "US_NYSE", "US_AMEX", "US_NYSE_AMERICAN", "US_ARCA"},
)

# ---------------------------------------------------------------------------
# 打分权重（加权和，各分项已归一化到 0~1），总和为 1
# ---------------------------------------------------------------------------
WEIGHTS = dict(
    gap_quality=0.22,     # gap 大小的"甜蜜区"，不是越大越好
    rvol=0.30,            # 盘前相对成交量 —— 最强的单一预测因子
    technical=0.20,       # 相对 52 周高点 / MA20 的位置，衡量上方套牢盘
    float_squeeze=0.14,   # 流通盘 + 融券可得量
    liquidity=0.14,       # 点差 + 名义成交额，决定滑点
)

# ---------------------------------------------------------------------------
# gap 甜蜜区：对数正态曲线。peak 处得 1.0，两侧衰减。
# 依据：极端 gap 的开盘价已经把催化剂 price in 完了，日内是净卖压。
# ---------------------------------------------------------------------------
GAP_CURVE = dict(peak_pct=12.0, sigma=0.85)

# 盘前美元 RVOL = pre_turnover / 20日平均日成交额
# floor 以下得 0 分，saturate 处得满分（对数刻度）
RVOL_SCALE = dict(floor=0.02, saturate=1.00)

# 流通股本分档（股）→ 逼空潜力得分
FLOAT_BUCKETS = [
    (10_000_000, 1.00),
    (25_000_000, 0.85),
    (75_000_000, 0.60),
    (200_000_000, 0.35),
    (float("inf"), 0.15),
]

# ---------------------------------------------------------------------------
# 催化剂乘数 —— 策略的核心门控
# ---------------------------------------------------------------------------
CATALYST_MULT = dict(
    hard=1.15,      # 确有重大事件，且方向与交易一致
    soft=1.00,      # 有事件但分量一般
    none=0.55,      # 找不到催化剂 = 纯资金推动，历史上回落概率最高
    negative=0.15,  # 催化剂方向与交易相反（做多遇增发 / 做空遇并购要约）
)

CATALYST_THRESHOLDS = dict(hard=0.70, soft=0.25, negative=-0.50)

# ---------------------------------------------------------------------------
# SEC 8-K item 分两类，这个区分很关键：
#
#   重要性(materiality) —— 方向中性。item 2.02 只说明"公司披露了业绩"，
#   并没有说业绩是好是坏；涨还是跌由 gap 本身告诉你。把它当利好用，
#   做空侧就会把"财报暴雷跌 27%"错判成反向信号。
#
#   方向性(directional) —— 自带符号。增发/退市/破产对做多是利空，
#   对做空恰恰是最好的催化剂。
# ---------------------------------------------------------------------------
EDGAR_MATERIALITY_ITEMS = {
    "2.02": 1.00,   # 经营业绩（财报）
    "1.01": 0.90,   # 签订重大协议（大单/合作/授权）
    "2.01": 0.85,   # 完成收购或处置
    "8.01": 0.60,   # 其他事项（FDA、临床数据常走这条）
    "7.01": 0.50,   # Reg FD 披露
    "5.02": 0.35,   # 高管/董事变动
    "5.07": 0.15,   # 股东投票结果
}

EDGAR_DIRECTIONAL_ITEMS = {
    "5.03": -0.30,  # 修改章程（常见于反向拆股）
    "3.03": -0.40,  # 修改证券持有人权利（常伴随融资）
    "1.02": -0.50,  # 终止重大协议
    "3.02": -0.70,  # 未注册股权发行（稀释）
    "3.01": -0.85,  # 退市 / 不符合上市标准
    "4.02": -0.90,  # 已发布财报不可依赖（重述）
    "1.03": -1.00,  # 破产
}

EDGAR_ITEM_WEIGHTS = {**EDGAR_MATERIALITY_ITEMS, **EDGAR_DIRECTIONAL_ITEMS}

# 8-K 里同时出现正负 item 时，负面到这个程度就一票否决。
# 依据：1.01(重大协议) + 3.02(未注册发行) + 3.03 + 5.03 是定增融资的典型组合，
# 此时 1.01 指的就是证券购买协议本身，按利好处理会做反方向。
EDGAR_NEGATIVE_OVERRIDE = -0.60
EDGAR_ITEM_NAMES = {
    "2.02": "财报", "1.01": "重大协议", "2.01": "完成并购", "8.01": "重大事项",
    "7.01": "Reg FD 披露", "5.02": "高管变动", "5.07": "股东投票",
    "1.02": "终止协议", "3.02": "股权增发", "3.01": "退市警告",
    "4.02": "财务重述", "1.03": "破产",
}
EDGAR_LOOKBACK_HOURS = 48

# ---------------------------------------------------------------------------
# 标题关键词词典。命中取最高权重（正）与最低权重（负），再合成。
# 负面词是这个策略最值钱的部分：小盘股盘前暴涨最常见的原因就是
# 「宣布增发」，那是做空信号，不是做多信号。
# ---------------------------------------------------------------------------
HEADLINE_POSITIVE = {
    # 并购 —— 最硬的催化剂，几乎不回落
    r"\b(to be acquired|acquisition of|agrees? to acquire|merger agreement|buyout|takeover bid|tender offer)\b": 1.00,
    # 生物医药
    r"\b(fda approval|fda approves|fda clearance|granted approval|breakthrough therapy|orphan drug|priority review)\b": 1.00,
    r"\b(positive (topline|top-line|phase|results)|meets? (the )?primary endpoint|phase 3 success)\b": 0.95,
    # 临床成功的其他常见表述 —— 漏掉这些会把 MRK 癌症疫苗那种级别的催化剂判成"无"
    r"\b(trial'?s? success|successful .{0,25}(trial|study)|trial (success|succeeds)|succeeds? in .{0,20}(trial|study))\b": 0.95,
    r"\b((pivotal|phase [123]|late-stage) (trial|study) (data|results|readout))\b": 0.85,
    r"\b(cancer vaccine|gene therapy) .{0,30}(success|positive|works|efficac)": 0.95,
    r"\b(hits?|achieves?|reaches?) .{0,20}endpoint\b": 0.90,
    # 财报 / 指引
    r"\b(raises? (fy\d*\s*)?(guidance|outlook|forecast)|lifts? outlook|boosts? (guidance|outlook))\b": 0.90,
    r"\b(beats?|tops?|surpass(es)?) (estimates|expectations|consensus|street)\b": 0.85,
    r"\b(q[1-4]|quarterly|fourth-quarter|third-quarter) (earnings|results|revenue)\b": 0.70,
    r"\b(record (revenue|quarter|results|bookings))\b": 0.75,
    # 商业进展
    r"\b(contract award|awarded (a )?contract|wins? .{0,20}contract|defense contract)\b": 0.90,
    r"\b(strategic partnership|partners? with|collaboration with|joint venture)\b": 0.65,
    # "被 X 选中/选用" 是拿下大客户最常见的写法，词典早先只认 partnership 会漏掉
    # （2026-08-25 实盘：RZLV +21% 靠 "Google Selects Rezolve AI's ... Platform"）
    r"\b(selected (by|for|to)|selects|chosen (by|for)|chooses|taps|tapped (by|to)|wins? (deal|order|mandate))\b": 0.70,
    r"\b(share repurchase|buyback|stock repurchase program)\b": 0.55,
    # 卖方
    r"\b(upgrade[sd]?|price target raised|raises? price target|initiated .{0,15}(buy|outperform|overweight))\b": 0.50,
}

HEADLINE_NEGATIVE = {
    # 稀释 —— 盘前暴涨 + 增发公告 = 高质量做空标的
    r"\b(public offering|registered direct|private placement|at-the-market|atm (offering|program)|underwritten offering)\b": -1.00,
    r"\b(pricing of|prices? \$?\d+.{0,15}(offering|shares)|announces? .{0,20}offering)\b": -1.00,
    r"\b(warrant inducement|dilut(ion|ive)|shelf registration)\b": -0.85,
    # 生存问题
    r"\b(reverse (stock )?split)\b": -0.80,
    r"\b(going concern|chapter 11|bankrupt(cy)?|delisting|deficiency letter|non-?compliance)\b": -0.95,
    r"\b(restat(es|ement)|accounting (error|irregularit))\b": -0.90,
    # 空头/无实质消息
    r"\b(short (seller|report)|fraud allegations)\b": -0.70,
    r"\b(downgrade[sd]?|price target (cut|lowered)|cuts? price target)\b": -0.45,
    r"\b(no (fresh |new |material )?news|limited fresh news|without news|no clear catalyst|unclear catalyst)\b": -0.60,
    r"\b(cuts? (guidance|outlook)|lowers? (guidance|outlook)|misses? estimates)\b": -0.85,
}

# 只看多少小时内的新闻
NEWS_LOOKBACK_HOURS = 36
NEWS_MAX_PER_TICKER = 8

# ---------------------------------------------------------------------------
# 交易计划
# ---------------------------------------------------------------------------
TRADE = dict(
    account_size=100_000.0,
    risk_per_trade_pct=0.5,     # 每笔风险占账户 0.5%
    atr_period=14,
    atr_stop_mult=1.0,          # 止损 = min(盘前低点, 入场 - 1.0*ATR)
    max_stop_pct=8.0,           # 止损距离上限，超过则放弃（风险回报不划算）
    targets_r=(1.0, 2.0, 3.0),  # 按 R 倍数的目标位
)

# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
RUNTIME = dict(
    host="127.0.0.1",
    port=11111,
    rank_pull=400,          # 从盘前榜拉取多少只（涨跌各一半）
    snapshot_batch=200,     # get_market_snapshot 单次上限 400，留余量
    enrich_top_n=60,        # 只对前 N 名拉历史 K 线（省配额）
    catalyst_top_n=35,      # 只对前 N 名查新闻 + EDGAR（省时间）
    final_n=15,             # 最终输出条数
    kline_days=120,         # 历史 K 线回溯天数（算 ATR/MA20/52周高）
    sec_user_agent="premkt-momentum-scanner antecetech@gmail.com",
)
