# Stock Monitor 桌面悬浮窗 v5.0

Windows 11 桌面股票监控悬浮窗，无边框、置顶、半透明、可拖拽。支持 A股、港股、美股、韩股实时行情，目标价 + 单日涨跌幅双预警。

## 功能特性

- **悬浮窗模式**：无边框置顶显示，半透明背景，鼠标拖拽移动，双击关闭
- **多市场支持**：A股（通达信/腾讯）、美股（Yahoo Finance + 腾讯）、港股、韩股
- **美股盘前盘后**：自动检测交易时段，显示盘前/盘后价格
- **双预警系统**：
  - 目标价预警（跌破/涨破/双向）
  - 单日涨跌幅预警（绝对值模式：设 3% 则涨3%或跌3%都触发）
- **预警变色**：触发条件时卡片变绿色，未触发为默认黑色，不闪烁
- **批量请求**：腾讯接口一次请求多只股票，减少网络开销
- **系统托盘**：最小化到托盘，双击托盘图标恢复窗口
- **可视化设置**：内置设置对话框，增删改监控股票、调整刷新间隔和数据源

## 快速开始

### 环境要求

- Windows 10/11
- Python >= 3.10
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

### 安装

```bash
# 使用 uv（推荐）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

### 运行

```bash
# 方式1：无窗口启动（推荐，无 cmd 黑框）
双击 run.vbs

# 方式2：命令行启动
uv run floating_window.py

# 方式3：bat 启动
双击 run.bat
```

## 使用说明

### 界面操作

| 操作 | 效果 |
|------|------|
| 单击拖拽 | 移动悬浮窗位置 |
| 双击窗口 | 关闭程序 |
| 鼠标悬停股票名称 | 显示完整名称（长名称会截断显示） |
| 点击 ↻ | 重新加载配置 |
| 点击 ⚙ | 打开设置对话框 |
| 双击托盘图标 | 恢复窗口 |

### 设置对话框

点击底部栏 ⚙ 按钮打开设置：

- **刷新间隔**：数据刷新周期（1-120 秒）
- **数据源**：auto（自动路由）/ tencent / yahoo / tdx
- **监控列表**：支持新增/删除/编辑股票
- **预警条件**：
  - 目标价 + 方向（跌破/涨破/双向）
  - 目标涨跌幅 + 方向（跌/涨/双向）

### 配置文件

配置保存在 `scripts/watchlist_config.json`：

```json
{
  "interval": 5,
  "source": "auto",
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
| `source` | 数据源：auto/tencent/yahoo/tdx |
| `code` | 股票代码 |
| `name` | 股票名称 |
| `target` | 目标价格（null 表示不监控） |
| `dir` | 价格方向：below/above/both |
| `target_pct` | 涨跌幅阈值（null 表示不监控） |
| `pct_dir` | 涨跌幅方向：below/above/both |
| `position` | 持仓信息（可选） |
| `note` | 备注（可选） |

## 支持的股票代码格式

| 市场 | 代码格式 | 示例 |
|------|----------|------|
| A股 | sh/sz + 6位数字 | sh601398、sz000001 |
| 美股 | 字母代码 | AAPL、QQQ、SPY |
| 港股 | hk + 数字 | hk00700 |
| 韩股 | kr + 数字 | kr005930 |

## 数据源说明

| 数据源 | 市场 | 延迟 | 特点 |
|--------|------|------|------|
| 通达信 (tdx) | A股 | ~15ms | 最低延迟，郑州节点 |
| 腾讯 (tencent) | A股/港股/美股 | ~155ms | 支持批量请求 |
| Yahoo Finance | 全球 | ~500ms | 支持盘前盘后数据 |
| auto | 自动路由 | - | 按市场自动选择最优源 |

## 项目结构

```
stock-monitor/
├── floating_window.py          # 主程序（悬浮窗 GUI）
├── scripts/
│   ├── price_alert.py          # 行情数据获取（多数据源）
│   └── watchlist_config.json   # 自选股配置
├── run.bat                     # Windows 启动脚本
├── run.vbs                     # 无窗口启动器
├── pyproject.toml              # 项目配置
└── requirements.txt            # 依赖列表
```

## 技术栈

- **GUI 框架**：PySide6（Qt for Python）
- **行情数据**：通达信协议（eltdx）、腾讯行情接口、Yahoo Finance API
- **Python**：>= 3.10
- **包管理**：uv / pip

## 依赖

- `PySide6 >= 6.5.0` — GUI 框架
- `eltdx >= 0.1.0` — 通达信行情协议

## License

MIT
