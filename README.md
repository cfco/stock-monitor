# Stock Monitor 桌面悬浮窗 v5.2

Windows 11 桌面 **A 股**监控悬浮窗：无边框、置顶、半透明、可拖拽。目标价 + 单日涨跌幅双预警，鼠标悬停显示 K 线图。

> **v5.2 起只支持 A 股（沪/深）**。此前的港股、美股、韩股支持（基于 Yahoo Finance）已整体移除——相关接口在实测中已不可用（详见「免费接口实测清单」），且与项目的轻量监控定位不符。

## 功能特性

- **悬浮窗模式**：无边框置顶显示，半透明背景，鼠标拖拽移动，双击任意位置关闭程序
- **纯 A 股行情**：五路免费接口轮询（腾讯 ×2 / 新浪 / 东方财富 ×2），避免单一接口限流
- **双预警系统**：
  - 目标价预警（跌破/涨破/双向）
  - 单日涨跌幅预警（设 3 表示涨 3% 或跌 3% 触发，方向可选「跌 / 涨 / 双向」）
- **预警变色**：触发条件时卡片变绿色，未触发保持默认色，不闪烁
- **K 线悬浮图**：
  - 鼠标悬停任意股票 0.3 秒后弹出，显示完整名称与最新价
  - 蜡烛图渲染，**红涨绿跌**（A 股习惯），标注最高/最低价和时间
  - 三档尺寸：小（320×180，默认）/ 中（640×360）/ 大（960×540）
  - 智能定位：自动避开屏幕边缘
  - 零延迟显示：K 线随行情预加载并缓存，悬停时直接读取
  - **双源保障**：东方财富优先，失败自动回退腾讯
- **批量请求**：每个接口一次请求多只股票，减少网络开销
- **紧凑布局**：窗口宽度 200-320px，单行卡片显示完整行情；最多显示 10 条，超出可滚动
- **可视化设置**：内置设置对话框，增删改监控股票、调整刷新间隔和数据源，保存即生效

## 快速开始

### 方式一：直接下载 exe（推荐，无需 Python）

到 [Releases](https://github.com/cfco/stock-monitor/releases) 下载 `StockMonitor.exe`，双击即可运行。

exe 由 GitHub Actions 自动构建（推 tag 触发），详见「自动打包」一节。

### 方式二：源码运行

环境要求：Windows 10/11、Python >= 3.10、[uv](https://docs.astral.sh/uv/)

```bash
uv sync                 # 创建 .venv 并安装依赖
uv run floating_window.py
```

也可以双击 `run.vbs`（无 cmd 黑框）或 `run.bat`。

## 使用说明

### 界面操作

| 操作 | 效果 |
|------|------|
| 单击拖拽空白区域 | 移动悬浮窗位置 |
| 双击窗口任意位置 | 关闭程序 |
| 鼠标悬停股票卡片 | 显示 K 线悬浮图（含完整名称和最新价，0.3 秒防抖） |
| 鼠标离开股票卡片 | K 线悬浮图自动隐藏 |
| 点击底部 ⚙ | 打开设置对话框（保存后自动刷新） |

### 设置对话框

- **刷新间隔**：数据刷新周期（1-120 秒）
- **K线图大小**：small / medium / large
- **数据源**：auto（自动轮询）/ tencent（强制腾讯）/ tdx（强制通达信）
- **TDX服务器**：可填自定义通达信节点，留空用内置节点
- **监控列表**：新增/删除/编辑股票
- **预警条件**：目标价 + 方向、目标涨跌幅 + 方向

## 支持的股票代码

| 市场 | 格式 | 示例 |
|------|------|------|
| 沪市 | `sh` + 6 位数字 | sh601398、sh510760（ETF） |
| 深市 | `sz` + 6 位数字 | sz000988、sz159915（ETF） |
| 指数 | `sh`/`sz` + 指数代码 | sh000001（上证指数） |

> 输入 6 位纯数字时会自动尝试补 `sh`/`sz` 前缀并校验代码是否存在。非 A 股代码（如 `AAPL`、`hk00700`）会被拒绝。

## 配置文件

配置保存在 `scripts/watchlist_config.json`：

```json
{
  "interval": 3,
  "kline_size": "large",
  "source": "auto",
  "theme": "auto",
  "tdx_host": "",
  "alerts": [
    {
      "code": "sh601398",
      "name": "工商银行",
      "target": 7.50,
      "dir": "below",
      "target_pct": 3.0,
      "pct_dir": "both",
      "position": "",
      "note": ""
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `interval` | 刷新间隔（秒） |
| `kline_size` | K线图大小：small/medium/large |
| `source` | 数据源：auto/tencent/tdx |
| `theme` | 主题：auto/dark/light |
| `tdx_host` | 自定义通达信节点（留空用内置） |
| `code` | 股票代码（sh/sz 前缀） |
| `name` | 股票名称 |
| `target` | 目标价格（null 表示不监控） |
| `dir` | 价格方向：below/above/both |
| `target_pct` | 涨跌幅阈值，**填正数**（如 `3` 表示 3%），null 表示不监控 |
| `pct_dir` | 涨跌幅方向：below(跌)/above(涨)/both(双向) |
| `position` | 持仓信息（可选） |
| `note` | 备注（可选） |

## 数据源

### A 股实时行情（五源轮询）

| # | 数据源 | 接口 | 实测延迟 | 特点 |
|---|--------|------|---------|------|
| 1 | 腾讯 | `qt.gtimg.cn` | ~91ms | 批量请求，最稳定，作为主源 |
| 2 | 新浪 | `hq.sinajs.cn` | ~67ms | 需 Referer 头，字段最全 |
| 3 | 东方财富 | `push2.eastmoney.com/api/qt/ulist.np` | ~92ms | 按 secid 批量，响应规整 |
| 4 | 腾讯备用 | `web.sqt.gtimg.cn` | — | 独立域名，与 #1 分开计频 |
| 5 | 东方财富历史 | `push2his.eastmoney.com` | ~105ms | 复用历史行情接口 |

轮询机制：每次请求按游标轮换下一个源，把请求压力摊到多家；某一源连续失败 5 次会被**临时屏蔽**，由其它源顶上，重启后恢复。

### A 股 K 线（双源）

| 角色 | 数据源 | 接口 |
|------|--------|------|
| 主 | 东方财富 | `push2his.eastmoney.com/api/qt/stock/kline/get`（klt=1/101/102） |
| 备 | 腾讯 | `ifzq.gtimg.cn/appstock/app/kline/{mkline,kline}` |

### 备用：通达信协议

`eltdx` 提供通达信协议直连（约 15ms），内置郑州/上海/北京三个节点，按优先级故障转移。需安装 `eltdx` 依赖。

## 免费接口实测清单（2026-09-22）

> 全部为真实请求实测结果，非资料整理。公开接口会不定期变更，接入前请以实测为准。

| 接口 | 实测结果 | 是否可用 |
|------|---------|---------|
| 腾讯 `qt.gtimg.cn` | HTTP 200，91ms | ✅ 已接入 |
| 新浪 `hq.sinajs.cn` | HTTP 200，67ms | ✅ 已接入 |
| 东方财富 `push2.eastmoney.com` | HTTP 200，92ms | ✅ 已接入 |
| 东方财富 `push2his.eastmoney.com` | HTTP 200，105ms | ✅ 已接入 |
| 腾讯 `ifzq.gtimg.cn`（1分/日/周 K 线） | HTTP 200，140ms | ✅ 已接入（K 线备用） |
| 腾讯 `proxy.finance.qq.com`（前复权日K） | HTTP 200，127ms | ✅ 可用，未接入 |
| 东方财富 `clist`（全市场排行） | HTTP 200 | ✅ 可用，未接入 |
| 同花顺 `d.10jqka.com.cn` | HTTP 200，但返回格式混杂 | ⚠️ 可用，解析成本高 |
| 网易财经 `api.money.126.net` | **DNS 解析失败** | ❌ 域名已失效 |
| 网易财经 `quotes.money.163.com` | **HTTP 502** | ❌ 不可用 |
| 百度股市通 `finance.pae.baidu.com` | **HTTP 403 hit risk** | ❌ 反爬拦截 |
| 雪球 `stock.xueqiu.com` | HTTP 200 但**返回 0 字节** | ❌ 需 cookie，实际不可用 |
| 腾讯 `web.ifzq.gtimg.cn` | **DNS 解析失败**（本机） | ❌ 该子域不可达 |

## 自动打包（GitHub Actions）

推一个 tag 即自动构建 Windows exe 并创建 Release：

```bash
git tag v5.2.0
git push github v5.2.0
```

工作流见 `.github/workflows/build-exe.yml`，也可以在 Actions 页面手动触发（`workflow_dispatch`）。

构建产物：`StockMonitor.exe`（单文件，无需安装 Python）。

## 项目结构

```
stock-monitor/
├── floating_window.py            # 主程序（悬浮窗 GUI + K 线悬浮图）
├── scripts/
│   ├── price_alert.py            # 行情数据获取（多源轮询 + K 线）
│   └── watchlist_config.json     # 自选股配置
├── .github/workflows/
│   └── build-exe.yml             # 自动打包 exe
├── run.bat / run.vbs             # Windows 启动脚本
├── pyproject.toml                # 项目配置
└── requirements.txt              # 依赖列表
```

## 技术栈

- **GUI**：PySide6（Qt for Python）
- **行情**：腾讯 / 新浪 / 东方财富 免费接口（A 股）
- **K 线**：东方财富（主）+ 腾讯（备）
- **通达信**：eltdx（可选备用源）
- **Python**：>= 3.10
- **依赖**：`PySide6-Essentials >= 6.5.0`、`eltdx >= 0.1.0`（可选）

## License

MIT
