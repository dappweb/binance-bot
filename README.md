# Binance Arbitrage Trading Bot

高性能币安套利交易机器人，专注于最大化套利收益。

## 功能特性

### 策略引擎
- **三角套利 (Triangular Arbitrage)**: 自动发现并执行三角套利机会
  - 例如: USDT → BTC → ETH → USDT，如果汇率差使得最终USDT增加，即为套利机会
  - 自动扫描所有可行的三角路径
  - 使用买卖盘口价格(bid/ask)进行精确利润计算
- **跨对套利 (Spread Arbitrage)**: 检测同一资产在不同稳定币对之间的价差
  - 例如: ETH/USDT vs ETH/BUSD 价差套利

### 核心组件
- **实时价格流**: WebSocket + REST API 双通道价格更新
- **高速执行引擎**: 毫秒级订单执行，支持市价单和限价单
- **风控管理**: 
  - 最大回撤保护
  - 每日亏损限额
  - 头寸大小限制
  - 自动交易暂停机制
- **实时仪表盘**: Rich终端界面实时显示
  - 当前套利机会
  - 执行历史及盈亏
  - 风控状态

## 项目结构

```
binance-bot/
├── main.py                    # 程序入口
├── config/
│   └── settings.py            # 配置管理 (Pydantic)
├── core/
│   ├── exchange.py            # Binance API 客户端
│   ├── price_stream.py        # 实时价格流管理
│   ├── executor.py            # 订单执行引擎
│   ├── risk_manager.py        # 风控管理
│   └── engine.py              # 主引擎编排
├── strategies/
│   ├── triangular_arb.py      # 三角套利策略
│   └── spread_arb.py          # 跨对价差套利
├── utils/
│   ├── logger.py              # 日志工具
│   ├── helpers.py             # 辅助工具函数
│   └── dashboard.py           # 终端仪表盘
├── tests/                     # 单元测试
├── .env.example               # 环境变量模板
└── requirements.txt           # Python 依赖
```

## 快速开始

### 1. 安装依赖

```bash
cd binance-bot
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的 Binance API Key：

```env
BINANCE_API_KEY=your_actual_api_key
BINANCE_API_SECRET=your_actual_api_secret
BINANCE_TESTNET=true    # 建议先用测试网
```

> 💡 **建议**: 先在 [Binance Testnet](https://testnet.binance.vision/) 测试

### 3. 运行

```bash
# 干跑模式 (默认，不执行真实交易)
python main.py

# 仅扫描模式 (显示套利机会)
python main.py --scan-only

# 自定义参数
python main.py --amount 50 --threshold 0.002

# 实盘交易 (小心！)
python main.py --live
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--live` | 启用实盘交易 | 否 (干跑) |
| `--scan-only` | 仅扫描不交易 | 否 |
| `--amount <值>` | 每笔交易USDT金额 | 100.0 |
| `--threshold <值>` | 最低利润阈值 (小数) | 0.001 (0.1%) |
| `--testnet` | 强制使用测试网 | 按.env配置 |
| `--log-level <级别>` | 日志级别 | INFO |

## 配置说明

在 `.env` 文件中可配置：

```env
# API 配置
BINANCE_API_KEY=xxx
BINANCE_API_SECRET=xxx
BINANCE_TESTNET=true

# 交易配置
TRADE_AMOUNT_USDT=100.0        # 每笔交易金额
MIN_PROFIT_THRESHOLD=0.001     # 最低利润率 (0.1%)
MAX_OPEN_ORDERS=5              # 最大同时订单数

# 风控配置
MAX_DRAWDOWN_PCT=5.0           # 最大回撤百分比
DAILY_LOSS_LIMIT_USDT=50.0     # 每日最大亏损
POSITION_SIZE_PCT=10.0         # 单笔头寸占比

# 高级配置
SCAN_INTERVAL=0.5              # 扫描间隔 (秒)
```

## 套利原理

### 三角套利
```
USDT ──买入BTC──> BTC ──买入ETH──> ETH ──卖出ETH──> USDT
 $100              0.002 BTC       0.0308 ETH       $101.54
                                                     利润: $1.54
```

当三条交易路径的价格乘积偏离1.0时，存在套利机会。扣除手续费后仍有正收益即可执行。

### 手续费考虑
- 标准手续费: 0.1% (每笔)
- BNB折扣: 0.075% (持有BNB自动启用)
- 三角套利需执行3笔交易: 总手续费 ≈ 0.3% (标准) 或 0.225% (BNB)

## 风险提示

⚠️ **重要风险提示**:

1. **市场风险**: 加密货币市场波动剧烈，套利窗口可能在执行过程中消失
2. **执行风险**: 网络延迟或API限制可能导致订单未能按预期价格成交
3. **滑点风险**: 市价单可能因流动性不足产生滑点
4. **技术风险**: 软件故障、网络中断等技术问题
5. **资金风险**: 请只使用您能承受损失的资金进行交易

**本软件仅供学习和研究目的。使用本软件进行实盘交易的风险由用户自行承担。**

## 运行测试

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## License

MIT
