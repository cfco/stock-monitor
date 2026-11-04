# Stock Monitor 桌面悬浮窗 v5.3.0

Windows 11 桌面 **A 股 + 美股** 监控悬浮窗：无边框、置顶、半透明、可拖拽。目标价 + 单日涨跌幅双预警，鼠标悬停显示 K 线图。

> **美股（Alpaca 免费版）**：夜盘实时行情走 WebSocket，历史 K 线与休市兜底走 REST，与 A 股卡片混排显示。
> 此前基于 Yahoo Finance 的美股支持已移除（接口实测不可用），现改由 Alpaca 接入。

## 功能特性

- **悬浮窗模式**：无边框置顶显示，半透明背景，鼠标拖拽移动，双击任意位置关闭程序
- **A 股行情**：五路免费接口轮询（腾讯 ×2 / 新浪 / 东方财富 ×2），避免单一接口限流
- **A 股代码统一校验**：大小写、`s_` 指数前缀自动归一为 `sh`/`sz` + 6 位数字；北交所（`bj`）和港股明确拒绝，不再静默加载成永远空白的卡片
- **数据源健康可见**：底栏 `源 x/5` 显示当前可用接口数；某源连续失败 5 次会临时屏蔽 300 秒，由其它源顶上，到期自动恢复
- **交易时段感知**：按 `Asia/Shanghai` 时区 + 内置中国节假日表判断；非交易时段不轮询、不触发预警，启动时仅展示最近收盘价并标记为非实时（不拿隔夜旧价误报）
- **预警一次性触发**：同一条件持续满足只提醒一次，价格回到安全侧后才允许再次提醒；跌破/涨破、涨/跌两个方向各自独立
- **运行日志与单实例**：日志写到数据目录 `logs/stock-monitor.log`（2MB × 3 轮转，打包后无控制台也能留证）；`QLockFile` 防止重复启动导致双份托盘图标、重复请求和配置互相覆盖
- **美股行情（Alpaca 免费版）**：
  - 与 A 股卡片**混排显示**，卡片带 `US` 市场标签，价格用 `$`，涨跌幅同为红涨绿跌
  - 实时数据走 **WebSocket**（IEX feed，含盘前盘后），仅在美东 04:00-20:00（夜盘）保持连接
  - 现价口径：**最近成交价优先**；盘前盘后买卖价差过宽（>1%）时不取中值，回退最近收盘价，避免挂单噪声显示成假涨跌
  - 历史 K 线与休市兜底走 **REST**（休市显示最近收盘价，本地 60s TTL 节流省配额）
  - K 线周期与 A 股一致可切换：分时 / 日K / 周K（时间统一显示北京时间）
  - 凭证 DPAPI 加密存本地（`secret_store.py`，勾选「启用美股」时弹窗录入），未配置时功能自动停用
- **双预警系统**：
  - 目标价预警（跌破/涨破/双向）
  - 单日涨跌幅预警（设 3 表示涨 3% 或跌 3% 触发，方向可选「跌 / 涨 / 双向」）
  - 触发后同一条件不重复弹窗；条件解除后再次满足才重新提醒
- **预警变色**：触发条件时卡片变绿色，未触发保持默认色，不闪烁
- **K 线悬浮图**：
  - 鼠标悬停任意股票 0.3 秒后弹出，显示完整名称与最新价
  - 蜡烛图渲染，**红涨绿跌**（A 股习惯），标注最高/最低价和时间
  - **A 股分时为当天全量数据**：从 9:30 开盘至当前时间的全部 1 分钟K线（数据源不含 9:15-9:25 集合竞价时段），按窗口宽度自适应压缩显示；日K/周K 仍为最近 60 根
  - 三档尺寸：小（320×180，默认）/ 中（640×360）/ 大（960×540）
  - 智能定位：自动避开屏幕边缘和悬浮窗本体
  - 零延迟显示：K 线随行情预加载并缓存，悬停时直接读取
  - **双源保障**：东方财富优先；其 HTTPS 握手被重置时同一请求自动回退 HTTP，仍失败再回退腾讯
  - 分时/日K/周K 均支持；接口异常时跳过坏记录，不让一只股票拖垮整张图
- **批量请求**：每个接口一次请求多只股票，减少网络开销；腾讯返回按 `v_<code>` 变量名严格映射，`sh000001` 与 `sz000001` 同数字代码不会错挂
- **紧凑布局**：窗口宽度 200-320px（默认 300），单行卡片显示完整行情；最多显示 10 条，超出可滚动
- **卡片新鲜度**：每张卡片右侧显示自己的最近成功取数时间（`HH:MM`，完整秒数在悬停与底栏）；非交易时段展示的是收盘价，价格与时间一并置灰并在悬停时标注「不参与预警」，不会把整晚不动的旧价当成实时价；窗口窄于 292px 时时间列整列隐藏而不是裁成半截；名称被截断时悬停可看全名 + 持仓 + 备注
- **可视化设置**：内置设置对话框，增删改监控股票、调整刷新间隔和数据源，保存即生效；保存前统一校验代码/价格/涨跌幅，非法项阻止保存并明确提示

## 快速开始

### 方式一：直接下载（推荐，无需 Python）

到 [Releases](https://github.com/cfco/stock-monitor/releases) 下载对应平台的包：

| 平台 | 文件 | 使用方式 |
|------|------|----------|
| Windows 10/11 | `StockMonitor.exe` | 双击运行 |
| Ubuntu 22.04 / 24.04 / WSL | `StockMonitor-linux-x64.tar.gz` | 解压后 `./StockMonitor` 运行 |

Ubuntu 首次运行前需安装 Qt 运行所需系统库（一次性；桌面版通常缺 libxcb-cursor0，WSL/minimal 还缺 GL/EGL）：

```bash
sudo apt-get install -y \
  libegl1 libgl1 libdbus-1-3 \
  libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 \
  libxcb-shape0 libxcb-randr0 libxcb-render-util0 \
  libxcb-xinerama0 libxcb-xkb1
tar -xzf StockMonitor-linux-x64.tar.gz
chmod +x StockMonitor
./StockMonitor
```

> Linux 说明：系统托盘需要桌面环境支持（GNOME 需装 AppIndicator 扩展），无托盘时关闭程序用「双击悬浮窗」；DPAPI 凭证加密仅 Windows 有效，Linux 下降级为 base64 编码保存（程序会打印提示）。
>
> **Wayland 会话**：悬浮窗贴右下角、K 线图按鼠标位置弹出并避开主窗口，都依赖客户端自己指定窗口坐标；而 xdg-shell 在协议层禁止这件事，`move()` 会被合成器静默丢弃（表现为窗口位置随机、K 线图盖住主窗口）。程序启动时若检测到 Wayland 且本机带 xcb 插件，会自动改走 **xcb（XWayland）**；日志里会打印 `[Platform]` 一行说明。想强制原生 Wayland 就显式设 `QT_QPA_PLATFORM=wayland`（接受定位失效），想强制 X11 就设 `QT_QPA_PLATFORM=xcb`。

所有包由 GitHub Actions 自动构建：**推 `master` 即构建并发一条预发布 Release**（`v{x.y.z}-dev.{N}`），推 tag 发正式版，也可手动触发；私有仓库只在建 tag 或手动触发时发 Release，详见「自动打包」一节。

### 方式二：源码运行

环境要求：Windows 10/11 或 Ubuntu 22.04+、Python >= 3.10、[uv](https://docs.astral.sh/uv/)

```bash
uv sync                 # 创建 .venv 并安装依赖
uv run floating_window.py
```

Windows 下也可以双击 `run.vbs`（无 cmd 黑框）或 `run.bat`。`run.bat` 只负责首次建环境和启动一次；程序自身有单实例锁，不会再像旧版那样每 5 秒重复拉起。


## 使用说明

### 界面操作

| 操作 | 效果 |
|------|------|
| 单击拖拽空白区域 | 移动悬浮窗位置 |
| 双击窗口任意位置 | 关闭程序 |
| 鼠标悬停股票卡片 | 显示 K 线悬浮图（含完整名称和最新价，0.3 秒防抖） |
| 鼠标移入 K 线悬浮图 | 悬浮图保持显示（移出后自动隐藏） |
| 双击 K 线悬浮图 | 关闭程序（与双击悬浮窗等效，大图悬在窗外也能直接双击关闭） |
| 鼠标离开股票卡片 | K 线悬浮图自动隐藏 |
| 点击底部 ⚙ | 打开设置对话框（保存后自动刷新） |

### 设置对话框

- **刷新间隔**：数据刷新周期（1-120 秒）
- **K线图大小**：small / medium / large
- **数据源**：auto（自动轮询）/ tencent（强制腾讯）/ tdx（强制通达信）
- **TDX服务器**：可填自定义通达信节点，留空用内置节点
- **美股（Alpaca）**：勾选「启用美股」时弹窗录入 API Key / Secret（设置页不显示任何账号信息）。凭证经 **Windows DPAPI 加密**后保存到本地配置（`api_key_enc`/`secret_enc`，绑定本机用户，非明文）；Linux/WSL 下自动降级为 base64 编码保存（程序打印提示，同样非明文）；取消勾选不清除凭证，重新启用时弹窗预填旧值直接沿用；旧版明文凭证在启动时自动迁移为密文
- **监控列表**：新增/删除/编辑股票
- **预警条件**：目标价 + 方向、目标涨跌幅 + 方向

## 支持的股票代码

| 市场 | 格式 | 示例 |
|------|------|------|
| 沪市 | `sh` + 6 位数字 | sh601398、sh510760（ETF） |
| 深市 | `sz` + 6 位数字 | sz000988、sz159915（ETF） |
| 指数 | `sh`/`sz` + 指数代码（也接受 `s_sh` 写法） | sh000001（上证指数）、s_sh000001 |
| 美股 | 1-5 位字母（支持类股） | AAPL、TSLA、BRK.B（也接受 usAAPL 写法） |

> 输入 6 位纯数字时会自动尝试补 `sh`/`sz` 前缀，并用行情接口查询名称；大小写和 `s_` 前缀会自动归一。
> 场内基金/ETF/LOF/REIT（`sh50`/`sh51`/`sh52`/`sh53`/`sh55`/`sh56`/`sh58`/`sz15`/`sz16`/`sz18`）价格统一显示 3 位小数，股票 2 位。号段按真实行情逐族核对，新号段上市后加进 `ETF_PREFIXES` 即可。
> 北交所（`bj######`）当前版本**明确不支持**：设置页会提示并拒绝保存，而不是加载后静默显示 `--`。
> 美股代码保存时统一转大写，需在设置中配置并启用 Alpaca 凭证后才有行情。
> `hk00700` 等港股代码仍不支持。

## 配置文件

配置保存在 `scripts/watchlist_config.json`（**只留在本机，不进版本库**：里面有你的自选股和加密后的凭证）。仓库里提供模板 `scripts/watchlist_config.example.json`，打包时随包释放的就是它：

```json
{
  "interval": 3,
  "kline_size": "large",
  "source": "auto",
  "theme": "auto",
  "tdx_host": "",
  "tdx_nodes": [],
  "alpaca": {
    "enabled": false,
    "api_key_enc": "（DPAPI 密文，base64）",
    "secret_enc": "（DPAPI 密文，base64）"
  },
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
| `source` | 数据源：auto/tencent/tdx（仅作用于 A 股） |
| `theme` | 主题：auto/dark/light |
| `tdx_host` | 自定义通达信节点（留空用内置；清空后会恢复内置节点，不再继续连旧地址） |
| `tdx_nodes` | 自定义通达信节点数组（可选，优先级低于 `tdx_host`；格式非法时整段忽略，不影响其它配置） |
| `alpaca.enabled` | 是否启用美股（Alpaca）行情 |
| `alpaca.api_key_enc` | Alpaca API Key ID 的 **加密密文**（勾选「启用美股」时弹窗录入，设置页不显示；Windows 用 DPAPI，Linux/WSL 降级为 base64） |
| `alpaca.secret_enc` | Alpaca Secret Key 的 **加密密文**（同上；DPAPI 密文绑定本机 Windows 用户，拷走也解不开） |
| `code` | 股票代码（A股 sh/sz 前缀，或美股 1-5 位字母） |
| `name` | 股票名称 |
| `target` | 目标价格（null 表示不监控） |
| `dir` | 价格方向：below/above/both |
| `target_pct` | 涨跌幅阈值，**填正数**（如 `3` 表示 3%），null 表示不监控 |
| `pct_dir` | 涨跌幅方向：below(跌)/above(涨)/both(双向) |
| `position` | 持仓信息（可选，鼠标悬停卡片名称时显示） |
| `note` | 备注（可选，鼠标悬停卡片名称时显示） |

> **配置版本说明**：5.3.0 起保存/加载都会经过 schema 归一：`interval` 限制在 1-120 秒，
> `source`/`theme`/`kline_size` 非法值回退默认，`alerts` 中代码非法（含北交所）、目标价非正数、
> 涨跌幅为 0 的条目会被跳过并打印原因；`tdx_nodes` 结构错误只丢弃该段。
> 局部坏项不会再触发整份配置备份/清空。配置格式按当前版本为准，不保证与旧版逐字段兼容。

## 数据源

### A 股实时行情（五源轮询）

| # | 数据源 | 接口 | 实测延迟 | 特点 |
|---|--------|------|---------|------|
| 1 | 腾讯 | `qt.gtimg.cn` | ~91ms | 批量请求，最稳定，作为主源 |
| 2 | 新浪 | `hq.sinajs.cn` | ~67ms | 需 Referer 头，字段最全 |
| 3 | 东方财富 | `push2.eastmoney.com/api/qt/ulist.np` | ~92ms | 按 secid 批量，响应规整 |
| 4 | 腾讯备用 | `web.sqt.gtimg.cn` | — | 独立域名，与 #1 分开计频 |
| 5 | 东方财富历史 | `push2his.eastmoney.com` | ~105ms | 复用历史行情接口 |

轮询机制：每次请求按游标轮换下一个源，把请求压力摊到多家；某一源连续失败 5 次会被**临时屏蔽 300 秒**，期间由其它源顶上，到期自动解封并清零计数。底栏会显示当前可用源数量。

坏行情（价格为 `0`/负数/`NaN`/`Infinity`）在进入界面前即被过滤；源健康判定还要求涨跌幅字段齐全，避免“能返回数字但其实是脏数据”的接口被当成可用。

### A 股 K 线（双源）

| 角色 | 数据源 | 接口 |
|------|--------|------|
| 主 | 东方财富 | `push2his.eastmoney.com/api/qt/stock/kline/get`（klt=1/101/102；HTTPS 失败自动回退 HTTP） |
| 备 | 腾讯 | `ifzq.gtimg.cn/appstock/app/kline/{mkline,kline}` |

### 备用：通达信协议

`eltdx` 提供通达信协议直连（约 15ms），内置郑州/上海/北京三个节点，按优先级故障转移。需安装 `eltdx` 依赖。

### 美股（Alpaca 免费版）

| 用途 | 方式 | 接口 | 说明 |
|------|------|------|------|
| 实时行情 | WebSocket | `wss://stream.data.alpaca.markets/v2/iex`（路径必须小写） | 订阅 trades + quotes，含盘前盘后，断线 5→60s 指数退避重连 |
| 历史 K 线 | REST | `data.alpaca.markets/v2/stocks/{symbol}/bars` | 分时/日K/周K，feed=iex，时间转北京时间 |
| 休市兜底 | REST | `/v2/stocks/bars`（日K）+ `/v2/stocks/quotes/latest` | 休市显示最近收盘价 + 涨跌幅，60s TTL 节流 |
| 股票名称 | REST | `/v2/assets`（交易 API：paper 地址优先，实盘兜底） | 全量拉一次进程内缓存，用于设置界面自动补名称 |

免费版约束（代码已内置适配）：实时仅 IEX feed（SIP 需付费）；REST 限流 200 次/分钟，本地限流器按 180/分钟控制；**美股数据统一走本地代理 `127.0.0.1:7890`**（直连访问不到 Alpaca，REST 与 WebSocket 都走该代理）；仅在美东 04:00-20:00（北京时间约 16:00/17:00-次日 08:00/09:00 夜盘时段）保持 WebSocket 连接。

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

发布规则按**仓库可见性**分流（GitHub 没有账号级的"公开仓自动发 Release"开关，规则写在 workflow 里，运行时用 API 查可见性）：

| 触发 | 公开仓库 | 私有仓库 |
|---|---|---|
| 推 `master` | 构建 + 发一条**预发布** Release（`v{x.y.z}-dev.{N}`）| 只构建，产物留在 Actions 运行页 |
| 推 `v*` tag | 构建 + 发**正式** Release | 同左 |
| 手动 `workflow_dispatch` | 构建 + 发 Release | 同左 |

开发构建标 `prerelease=true` 且 `make_latest=false`，所以 `/releases/latest` 始终指向真正的正式版本。开发版 tag 的版本号取自 `pyproject.toml`，`{N}` 是 workflow 运行号，单调递增不撞车。

发正式版本：

```bash
git tag v5.3.0
git push github v5.3.0
```

工作流见 `.github/workflows/build-exe.yml`（`test` + `meta` → `build-windows` / `build-linux`：单测不过不出包；`meta` 一次算清可见性与发布参数，两个构建 job 只读结果，避免判断逻辑在两处漂移）。打包定义只有一份 `StockMonitor.spec`，本地与 CI 都执行 `pyinstaller --noconfirm --clean StockMonitor.spec`；CI 打包前会用模板配置覆盖本机配置，产物里不会带上你的自选股。

构建产物：
- `StockMonitor.exe` — Windows 单文件（无需安装 Python）
- `StockMonitor-linux-x64.tar.gz` — Linux 单文件二进制（ubuntu-22.04 构建，glibc 2.35，兼容 22.04/24.04/WSL；smoke test 在 xvfb 无显示环境下验证启动）

## 项目结构

```
stock-monitor/
├── floating_window.py            # 主程序（悬浮窗 GUI + K 线悬浮图 + 市场分流）
├── scripts/
│   ├── price_alert.py            # A股行情数据获取（多源轮询 + K 线）
│   ├── alpaca_history.py         # 美股历史数据（REST：K 线 / 快照 / 昨收 / 名称）
│   ├── alpaca_stream.py          # 美股实时行情（WebSocket + 统一取数入口）
│   ├── secret_store.py           # 本地凭证 DPAPI 加密存储（Alpaca Key/Secret）
│   ├── watchlist_config.json     # 本机自选股配置（含加密凭证，不入库）
│   └── watchlist_config.example.json  # 配置模板（随包释放的默认值）
├── .github/workflows/
│   └── build-exe.yml             # 自动打包 exe
├── run.bat / run.vbs             # Windows 启动脚本（run.bat 仅启动一次）
├── tests/                        # pytest 回归测试（A 股数据层 / GUI / 美股）
├── logs/                         # 运行时日志（自动创建，不入库）
├── pyproject.toml                # 项目配置
└── requirements.txt              # 依赖列表
```

## 技术栈

- **GUI**：PySide6（Qt for Python）
- **行情**：腾讯 / 新浪 / 东方财富 免费接口（A 股）
- **K 线**：东方财富（主）+ 腾讯（备）
- **美股**：Alpaca 免费版（实时 WebSocket + 历史 REST，IEX feed）
- **通达信**：eltdx（可选备用源）
- **Python**：>= 3.10
- **依赖**：`PySide6-Essentials >= 6.5.0`、`eltdx >= 0.1.0`（可选）、`websocket-client >= 1.9.2`（1.9.x 起必须显式给 `proxy_type`）、`tzdata >= 2026.5`（Windows 下 zoneinfo 时区库）

## License

MIT
