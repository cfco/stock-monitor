# Stock Monitor 优化计划

## Summary
对 stock-monitor 项目进行代码优化，涵盖6个改进项：系统托盘预警通知、配置文件编码修复、TDX节点配置外置、HTTP→HTTPS升级、清理空调试日志、运行时长显示格式修正。

---

## 当前状态分析

### 2.1 预警通知缺失
[floating_window.py:1811](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1811) `on_alert()` 是空函数，QuoteWorker 已通过 `alert_triggered` Signal 发出预警数据但UI层无任何响应。

### 2.2 配置文件编码不一致
[floating_window.py:117](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L117) `load_config()` 用 `utf-8-sig`（带BOM），[L126](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L126) `save_config()` 用 `utf-8`（无BOM）。通过UI保存的配置写入后可能被外部编辑器以错误编码读取。

### 2.3 TDX节点硬编码
[price_alert.py:75-82](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py#L75-L82) TDX_NODES 和 DEFAULT_TDX_HOST 硬编码在源码中，用户无法在不修改代码的情况下更换节点。

### 2.4 HTTP未加密请求
[price_alert.py:863/926/1180](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py#L863) 东方财富3个接口使用明文HTTP，有中间人风险。

### 2.7 调试日志空函数调用
[floating_window.py:20](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L20) `_log()` 定义为空函数，14处调用均为无副作用的调试桩，应直接删除。

---

## 修改方案

### 改动1: 系统托盘预警通知 (2.1)

**文件**: [floating_window.py](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py)

#### 1.1 添加 QSystemTrayIcon 和 QPixmap 导入
在 QtGui 导入行添加 `QPixmap`：
```python
from PySide6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPainter, QBrush, QPen,
    QMouseEvent, QPaintEvent, QAction, QIntValidator, QDoubleValidator, QCursor,
    QPixmap,    # <-- 新增
)
```
在 QtWidgets 导入行添加 `QSystemTrayIcon`：
```python
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QFrame, QGraphicsDropShadowEffect, QScrollArea,
    QMessageBox, QSizePolicy,
    QDialog, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFormLayout, QGroupBox, QSpinBox,
    QSystemTrayIcon,   # <-- 新增
)
```

#### 1.2 在 FloatingWindow.__init__ 初始化托盘
在 `self._settings_dialog = None` 之后添加：
```python
        # 系统托盘
        self._tray_icon = None
        self._build_tray_icon()
```

#### 1.3 添加托盘相关方法
在 `_move_to_bottom_right` 前（约 L1590）插入以下方法：

```python
    def _build_tray_icon(self):
        """创建系统托盘图标"""
        pixmap = self._create_tray_pixmap()
        self._tray_icon = QSystemTrayIcon(QIcon(pixmap), self)
        self._tray_icon.setToolTip('Stock Monitor')
        self._tray_icon.activated.connect(self._on_tray_activated)

        menu = QMenu()
        show_action = QAction('显示悬浮窗', menu)
        show_action.triggered.connect(self.show)
        quit_action = QAction('退出', menu)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.show()

    def _create_tray_pixmap(self):
        """创建托盘图标: 红色圆形+白色上涨箭头"""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor('#e74c3c'))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(4, 4, size - 8, size - 8)
        painter.setPen(QPen(QColor('#ffffff'), 3))
        painter.drawLine(32, 44, 32, 20)
        painter.drawLine(24, 28, 32, 20)
        painter.drawLine(40, 28, 32, 20)
        painter.end()
        return pixmap

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.show()
            self.raise_()
            self.activateWindow()
```

#### 1.4 重写 on_alert 方法
将 [L1811-1812](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1811-L1812) 替换为：

```python
    def on_alert(self, data):
        """预警触发时显示系统托盘通知并唤醒窗口"""
        name = data.get('name', '')
        message = data.get('message', '')
        price = data.get('price', 0)
        if self._tray_icon and self._tray_icon.isSystemTrayAvailable():
            self._tray_icon.showMessage(
                f'{name} 预警触发',
                f'{message} | 当前价: {price:.2f}',
                QSystemTrayIcon.Warning,
                5000,
            )
        self.show()
        self.raise_()
        self.activateWindow()
```

---

### 改动2: 配置文件编码一致性 (2.2)

**文件**: [floating_window.py](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py)

#### 修改 save_config 函数
将 [L124-131](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L124-L131) 中编码改为 `'utf-8-sig'`：

```python
def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8-sig') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'Error saving config: {e}')
        return False
```

---

### 改动3: TDX节点配置外置 (2.3)

**涉及文件**: [scripts/watchlist_config.json](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/watchlist_config.json), [scripts/price_alert.py](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py), [floating_window.py](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py), [floating_window.py](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py) SettingsDialog

#### 3.1 price_alert.py — 支持从外部传入 TDX_NODES

将 [L75-82](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py#L75-L82) 的硬编码常量改为可配置：

```python
# 默认 TDX节点列表（可通过 watchlist_config.json 的 tdx_nodes 字段覆盖）
DEFAULT_TDX_NODES = [
    {'id': 'zhengzhou_z1', 'name': '云行情郑州主站Z1', 'host': '182.118.8.4:7709', 'priority': 1},
    {'id': 'shanghai_z1',  'name': '云行情上海主站Z1', 'host': '114.141.177.44:7709', 'priority': 2},
    {'id': 'beijing_z1',   'name': '云行情北京主站Z1', 'host': '123.125.108.103:7709', 'priority': 3},
]
DEFAULT_TDX_HOST = '182.118.8.4:7709'
DEFAULT_TDX_NODE_NAME = '云行情郑州主站Z1'

# 运行时TDX节点列表（可被配置覆盖）
TDX_NODES = DEFAULT_TDX_NODES
```

在 `get_tdx_client()` 中添加参数支持（保留向后兼容）：
```python
def get_tdx_client(host=None, nodes=None):
    """获取或创建eltdx客户端，支持自定义节点列表"""
    global _tdx_client, _current_node, TDX_NODES
    if nodes is not None:
        TDX_NODES = nodes
    ...（其余逻辑不变）
```

#### 3.2 watchlist_config.json — 添加 tdx_nodes 字段
```json
{
  "alerts": [...],
  "interval": 3,
  "source": "auto",
  "kline_size": "large",
  "theme": "auto",
  "tdx_nodes": null
}
```
`null` 表示使用内置默认节点。

#### 3.3 floating_window.py — load_config 提取 tdx_nodes
在 [L115-122](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L115-L122) 的 `load_config()` 中增加处理：
```python
def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            config = json.load(f)
        # 处理TDX节点配置
        custom_nodes = config.get('tdx_nodes')
        if custom_nodes and isinstance(custom_nodes, list):
            from price_alert import TDX_NODES as _tn
            import price_alert
            price_alert.TDX_NODES = custom_nodes
            price_alert.DEFAULT_TDX_HOST = custom_nodes[0]['host']
        return config
    except Exception as e:
        print(f'Error loading config: {e}')
        return {'interval': 5, 'source': 'auto', 'alerts': [], 'theme': 'auto'}
```

#### 3.4 floating_window.py — SettingsDialog 添加 TDX节点管理
在 SettingsDialog 的"全局设置"区域（[L1118-1121](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1118-L1121) 主题行之后）添加 TDX节点配置：

```python
        self.tdx_host_edit = QLineEdit()
        self.tdx_host_edit.setText(self.config.get('tdx_host', ''))
        self.tdx_host_edit.setPlaceholderText('留空使用默认节点')
        top_form.addRow('TDX服务器:', self.tdx_host_edit)
```

在 `save_settings` 方法中保存该值：
```python
        self.config['tdx_host'] = self.tdx_host_edit.text().strip()
```

在 [L1414-1416](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1414-L1416) 保存逻辑中增加：
```python
        # 同步TDX节点到price_alert模块
        tdx_host = self.config.get('tdx_host', '')
        if tdx_host:
            import price_alert
            price_alert.TDX_NODES = [
                {'id': 'custom', 'name': f'自定义节点({tdx_host})', 'host': tdx_host, 'priority': 1}
            ]
            price_alert.DEFAULT_TDX_HOST = tdx_host
```

在 `FloatingWindow.start_monitor` 中传递配置：
```python
    def start_monitor(self):
        if self.worker and self.worker.isRunning():
            return
        self.config = load_config()
        # 应用TDX配置
        tdx_host = self.config.get('tdx_host', '')
        if tdx_host:
            import price_alert
            price_alert.TDX_NODES = [
                {'id': 'custom', 'name': f'自定义({tdx_host})', 'host': tdx_host, 'priority': 1}
            ]
            price_alert.DEFAULT_TDX_HOST = tdx_host
        ...（后续逻辑不变）
```

---

### 改动4: HTTP → HTTPS (2.4)

**文件**: [scripts/price_alert.py](file:///c:/Users/Administrator/Desktop/stock-monitor/scripts/price_alert.py)

将3处 HTTP 升级为 HTTPS：

**L863** (`_fetch_one` 函数内):
```python
# 原: http://push2his.eastmoney.com
url = f'https://push2his.eastmoney.com/api/qt/stock/get...'
```

**L926** (`get_quotes_batch` 东方财富批量):
```python
# 原: http://push2.eastmoney.com
url = f'https://push2.eastmoney.com/api/qt/ulist.np/get...'
```

**L1180** (`get_kline` 东方财富K线):
```python
# 原: http://push2his.eastmoney.com
url = f'https://push2his.eastmoney.com/api/qt/stock/kline/get...'
```

保留原注释说明，因历史上可能存在TLS握手问题，但近年各服务器已基本支持HTTPS。

---

### 改动5: 清理空 _log() 函数调用 (2.7)

**文件**: [floating_window.py](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py)

删除 [L20-21](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L20-L21) 的空函数定义：
```python
# 调试日志(空实现,避免修改所有调用处)
def _log(msg):
    pass
```

删除全部14处 `_log(...)` 调用：
- [L982](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L982): `_log(f'[Thread] run() 开始...')`
- [L1033](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1033): `_log(f'[Thread] run() 完成...')`
- [L1036](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1036): `_log(f'[Thread] run() 异常...')`
- [L1056](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1056): `_log('[SettingsDialog] 初始化完成...')`
- [L1290](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1290): `_log(f'[cellChanged] row=...')`
- [L1300](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1300): `_log(f'[cellChanged] code="..."')`
- [L1306](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1306): `_log(f'[cellChanged] 已带前缀...')`
- [L1323](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1323): `_log(f'[cellChanged] 线程已启动')`
- [L1327](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1327): `_log(f'[resolve_done] candidates=...')`
- [L1333](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1333): `_log(f'[resolve_done] item=...')`
- [L1340](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1340): `_log(f'[resolve_done] 单候选...')`
- [L1344](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1344): `_log(f'[resolve_done] 多候选...')`
- [L1351](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1351): `_log(f'[resolve_done] 弹窗结果...')`
- [L1361](file:///c:/Users/Administrator/Desktop/stock-monitor/floating_window.py#L1361): `_log(f'[resolve_done] 已填充...')`

---

## 验证步骤

1. **语法检查**: `python -m py_compile floating_window.py && python -m py_compile scripts/price_alert.py`
2. **功能验证**:
   - 启动程序，确认悬浮窗正常显示，托盘图标出现在系统托盘区
   - 修改 watchlist_config.json 中目标价触发预警，确认托盘弹出通知
   - 点击托盘图标，确认窗口恢复显示
   - 通过UI设置对话框修改TDX服务器地址并保存，重启后生效
   - 修改配置文件并保存，确认文件头仍为UTF-8 BOM（EF BB BF）
   - 确认东方财富接口仍正常工作（通过终端打印的行情数据判断）
   - 确认删除_log调用后程序行为无变化
3. **回归**: run.bat / run.vbs 启动方式保持不变
