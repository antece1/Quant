# 部署

## 先决条件

1. **moomoo OpenD 必须在跑并且已登录行情。** 这是整套东西的单点故障，
   而且它的失败模式是**静默返回空数据**而不是报错 —— 未登录时扫描器照常
   跑完、照常输出表格，只是内容是空的。所以任何自动化都必须先过自检。
2. Python 环境在 `Claude Code/.venv`（不要装进 anaconda base）。

## 每次跑之前：自检

```bash
python -m premkt.preflight
```

检查 OpenD 进程 / 端口 / 登录态 / 订阅额度 / 真实取一次快照 / 数据新鲜度 / 当前时段。
退出码：`0` 就绪，`1` OpenD 不可用，`2` 能连但数据有问题，`3` 时段不匹配。

## 统一入口

```bash
./premkt/run.sh <module> [args]
```

先跑自检，通过才执行，全程写 `premkt/logs/<module>_<日期>.log`，日志保留 30 天。
加 `REQUIRE_SESSION=premarket` 可以要求特定时段，不匹配就跳过（退出码 0，
定时任务不会报错）。

## 该自动化的 vs 该手动跑的

| | 工具 | 为什么 |
|---|---|---|
| **自动化** | `scan --save`、`evaluate` | 批处理，跑完出结果，无人值守没问题 |
| **手动** | `volspike`、`odte_radar`、`optflow`、`dual` | 常驻监控和临时查询，本质是给人看的；塞进定时任务只会变成没人看的日志 |

前向验证依赖每天的快照积累，所以 `scan --save` 是**最值得自动化的一个** ——
漏一天就少一天样本，而权重标定需要 100 笔以上。

## 安装定时任务

```bash
./premkt/deploy/install.sh install     # 装
./premkt/deploy/install.sh status      # 看状态
./premkt/deploy/install.sh uninstall   # 卸
```

两个任务（周一到周五）：

| 任务 | 本地时间 (PT) | 美东 | 动作 |
|---|---|---|---|
| `com.premkt.scan` | 05:15 | 08:15 | `scan --save`，要求盘前时段 |
| `com.premkt.evaluate` | 13:15 | 16:15 | `evaluate`，要求盘后时段 |

**时区注意**：launchd 用本机本地时间，本机是 PT，美东 = PT + 3。
如果你的机器换了时区，plist 里的 `Hour` 要跟着改。

## 盘中的手动流程

```bash
# 开盘前
./premkt/run.sh scan --save

# 盘中（开单独的终端窗口，它们会一直跑）
./premkt/run.sh volspike            # 买量爆炸提醒，带今日回补
./premkt/run.sh odte_radar          # 0DTE 期权流，30 秒一刷

# 随时查单只票
./premkt/run.sh optflow US.TSLA
./premkt/run.sh dual                # 动量+波动双口径

# 收盘后
./premkt/run.sh evaluate
```

## 需要留意的运行时限制

- **OpenD 会重启**（实测 pid 变过）。重启后订阅额度清空，是好事；但如果它挂了
  没起来，定时任务会以退出码 1 中止并留日志。
- **历史 K 线额度**：7 天窗口内 100 只不同股票。扫描器一次 50 只，两天就打满。
  已内置回退到订阅通道（`get_cur_kline`），不消耗该额度。
- **订阅额度**：总共 100，**期权只有 20**。monitors 用完会退订；
  如果异常退出留下残留订阅，重启 OpenD 可清空。
- **限频**：`get_search_news` 和 `get_stock_filter` 都是 10 次/30 秒，
  已在代码里节流。同时跑多个 monitor 会共享这些额度，可能互相拖慢。

## 日志

```bash
tail -f premkt/logs/volspike_$(date +%F).log
ls -lt premkt/logs/ | head
```
