#!/usr/bin/env python3
"""
floating_window.py — Stock Monitor 桌面悬浮窗 v5.2.1
PySide6 原生 Win11 风格悬浮窗：无边框、置顶、半透明、可拖拽
数据获取复用 price_alert.py（A股）+ alpaca_*.py（美股 Alpaca 免费版）：
  - A股：五路免费接口轮询（腾讯/新浪/东方财富/通达信）
  - 美股：实时=WebSocket 推送（alpaca_stream），历史K线/休市兜底=REST（alpaca_history）
紧凑版：1/3 宽度，支持目标价 + 单日涨跌% 双预警
"""

import os
import sys
import re
import json
import time
import datetime
import threading

def _resource_dir():
    """只读资源目录：源码运行时是项目根，打包成 exe 后是 PyInstaller 解包目录。"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """可写数据目录：必须是用户看得见、重启不丢的位置。

    打包后不能再用 __file__ 定位配置文件 —— 那指向 PyInstaller 的临时解包目录，
    程序一退出就被清理，用户改好的自选股会莫名丢失。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


sys.path.insert(0, os.path.join(_resource_dir(), 'scripts'))
from price_alert import close_tdx_client, get_quotes_batch, get_kline_1min
# 美股（Alpaca 免费版）：实时=WebSocket（alpaca_stream），历史/兜底=REST（alpaca_history）
import alpaca_history
import alpaca_stream
import secret_store          # 本地凭证 DPAPI 加密（Alpaca Key/Secret）


# 美股代码规则：1-5 位字母，可带类股后缀（如 AAPL / BRK.B）
_US_SYMBOL_RE = re.compile(r'^[A-Za-z]{1,5}(\.[A-Za-z]{1,2})?$')


def normalize_us_symbol(code):
    """把输入归一化为 Alpaca 美股代码；非美股返回空串

    规则：
      - 直接匹配 1-5 位字母（AAPL、USB、BRK.B）→ 大写返回
      - 支持腾讯式 us 前缀写法（usAAPL → AAPL）：整体不匹配时剥掉 us 前缀再试
      - A 股代码（sh/sz/bj + 数字）含数字，天然不匹配本规则，不会误判
    """
    c = (code or '').strip()
    if not c:
        return ''
    if _US_SYMBOL_RE.match(c):
        return c.upper()
    if c[:2].lower() == 'us' and _US_SYMBOL_RE.match(c[2:]):
        return c[2:].upper()
    return ''


def is_us_symbol(code):
    """判断是否为美股代码（行情分流 / 货币符号 / 市场标签共用的单一判定入口）"""
    return bool(normalize_us_symbol(code))

from PySide6.QtCore import Qt, QThread, Signal, QPoint, QRect, QTimer, QSize
from PySide6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPainter, QBrush, QPen,
    QMouseEvent, QPaintEvent, QAction, QIntValidator, QDoubleValidator, QCursor,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QFrame, QGraphicsDropShadowEffect, QScrollArea,
    QMessageBox, QSizePolicy,
    QDialog, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFormLayout, QGroupBox, QSpinBox, QCheckBox,
    QSystemTrayIcon, QMenu,
)

# ============================================================
# 配置
# ============================================================
VERSION = "5.2.1"
# 源码运行：项目内 scripts/watchlist_config.json
# 打包 exe：exe 同级的 watchlist_config.json（用户可直接编辑，重启不丢）
CONFIG_FILE = (
    os.path.join(_data_dir(), 'watchlist_config.json')
    if getattr(sys, 'frozen', False)
    else os.path.join(_data_dir(), 'scripts', 'watchlist_config.json')
)

# 颜色 - 涨跌色（两套主题共用）
COLOR_UP = '#e74c3c'
COLOR_DOWN = '#2ecc71'
COLOR_FLAT = '#95a5a6'
COLOR_ALERT = '#ff5555'
COLOR_TRIGGERED = '#00e676'  # 预警触发荧光绿

# 双主题
THEME_DARK = {
    'bg': '#1a1a2e',
    'bg_light': '#252540',
    'text': '#ffffff',
    'text_dim': '#aaaaaa',
    'border': '#3a3a55',
    'default_card': '#1a1a2e',
}
THEME_LIGHT = {
    'bg': '#f5f5f5',
    'bg_light': '#e8e8e8',
    'text': '#1a1a1a',
    'text_dim': '#666666',
    'border': '#cccccc',
    'default_card': '#ffffff',
}

# 当前主题（启动时默认深色）
_current_theme = 'dark'
_theme = THEME_DARK

def get_color(key):
    """获取当前主题颜色"""
    return _theme.get(key, THEME_DARK[key])

def set_theme(theme_name):
    """切换主题: 'dark' 或 'light'"""
    global _current_theme, _theme
    _current_theme = theme_name
    _theme = THEME_DARK if theme_name == 'dark' else THEME_LIGHT

def get_current_theme():
    return _current_theme

# 兼容性: 保留 COLOR_BG 等名称但改为动态函数调用
# 由于代码中大量使用 f'{COLOR_BG}' 拼接，这里用属性对象保持兼容
class _ColorProxy:
    """代理对象，str()时返回当前主题对应颜色"""
    def __init__(self, key):
        self.key = key
    def __str__(self):
        return get_color(self.key)
    def __repr__(self):
        return get_color(self.key)
    def __format__(self, format_spec):
        return get_color(self.key)

COLOR_BG = _ColorProxy('bg')
COLOR_BG_LIGHT = _ColorProxy('bg_light')
COLOR_TEXT = _ColorProxy('text')
COLOR_TEXT_DIM = _ColorProxy('text_dim')
COLOR_BORDER = _ColorProxy('border')
COLOR_DEFAULT_CARD = _ColorProxy('default_card')

CRITICAL_THRESHOLD = 0.01

# 方向映射：中文显示 ↔ 英文存储
DIR_LABELS = {'below': '跌破', 'above': '涨破', 'both': '双向'}
DIR_VALUES = {'跌破': 'below', '涨破': 'above', '双向': 'both'}
PCT_DIR_LABELS = {'below': '跌', 'above': '涨', 'both': '双向'}
PCT_DIR_VALUES = {'跌': 'below', '涨': 'above', '双向': 'both'}


def check_pct_alert(change_pct, target_pct, pct_dir):
    """单日涨跌幅预警的唯一判定入口（QuoteWorker 与 QuoteCard 共用）。

    口径约定：用户填正数 3 表示「3%」，方向由 pct_dir 决定 ——
      below = 跌幅达到、above = 涨幅达到、both = 涨跌任一达到。

    返回 (是否触发, 方向中文)。

    为什么要有这个函数：原先判定逻辑在 _check_pct_alert 与 QuoteCard.set_data
    里各写了一遍，且 below 分支写成 `change_pct <= target_pct`（没取负），
    用户填 3 时 `change_pct <= 3` 连「涨 1%」都会命中 —— 误触发。
    抽成单一入口后口径只有一处，不会再分叉。
    """
    if change_pct is None or target_pct is None:
        return False, ''
    abs_target = abs(target_pct)
    if pct_dir == 'below':
        if change_pct <= -abs_target:          # 跌幅达到阈值（填正数 3 表示跌 3%）
            return True, '跌'
    elif pct_dir == 'above':
        if change_pct >= abs_target:           # 涨幅达到阈值
            return True, '涨'
    elif abs(change_pct) >= abs_target:        # both/未知：涨跌任一达到
        return True, '涨' if change_pct >= 0 else '跌'
    return False, ''


def check_price_alert(price, target, direction):
    """目标价预警的唯一判定入口（QuoteWorker 与 QuoteCard 共用）。

    方向约定：below=跌破, above=涨破, both=双向。
    返回 (是否触发, 方向中文)。

    原先 _check_alert 与 QuoteCard.set_data 里各写一遍 `price <= target` 判定，
    两处口径可能再次分叉；抽成单一入口后判定逻辑只有一处。
    """
    if price is None or target is None:
        return False, ''
    if direction in ('below', 'both') and price <= target:
        return True, DIR_LABELS.get('below', '跌破')
    if direction in ('above', 'both') and price >= target:
        return True, DIR_LABELS.get('above', '涨破')
    return False, ''


# ============================================================
# 配置读写
# ============================================================
def load_config():
    # 打包后首次运行：exe 同级还没有配置文件，先把随包资源释放一份出来，
    # 否则用户打开就是空列表，还得自己一个个加
    if not os.path.exists(CONFIG_FILE):
        bundled = os.path.join(_resource_dir(), 'scripts', 'watchlist_config.json')
        if os.path.exists(bundled):
            try:
                with open(bundled, 'r', encoding='utf-8-sig') as src:
                    data = json.load(src)
                with open(CONFIG_FILE, 'w', encoding='utf-8-sig') as dst:
                    json.dump(data, dst, ensure_ascii=False, indent=2)
                print(f'[Config] 已释放默认配置到 {CONFIG_FILE}')
            except Exception as e:
                print(f'[Config] 释放默认配置失败: {e}')

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            config = json.load(f)
        # 旧版明文凭证 → DPAPI 密文迁移（幂等；有变更才落盘一次）
        try:
            if secret_store.migrate_config(config):
                save_config(config)
        except Exception as e:
            print(f'[Config] 凭证迁移失败(不影响启动): {e}')
        # 处理TDX节点配置
        custom_nodes = config.get('tdx_nodes')
        if custom_nodes and isinstance(custom_nodes, list):
            import price_alert
            price_alert.TDX_NODES = custom_nodes
            price_alert.DEFAULT_TDX_HOST = custom_nodes[0]['host']
        return config
    except Exception as e:
        print(f'Error loading config: {e}')
        # 损坏配置先改名留证再回退默认值：否则随后任何一次保存（含美股自动补名）
        # 都会把空 alerts 覆写上去，自选股整份不可恢复
        try:
            backup = CONFIG_FILE + '.broken-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            os.replace(CONFIG_FILE, backup)
            print(f'[Config] 损坏配置已备份到 {backup}，可手工修复后重启')
        except OSError as rename_err:
            print(f'[Config] 备份损坏配置失败: {rename_err}')
        return {'interval': 5, 'source': 'auto', 'alerts': [], 'theme': 'auto'}


def save_config(config):
    """原子写：先写同目录临时文件再替换，避免进程在写入中途被杀留下半截 JSON"""
    tmp = CONFIG_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8-sig') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
        return True
    except Exception as e:
        print(f'Error saving config: {e}')
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def is_a_share_trading_time(now=None):
    """判断当前是否在A股交易时间段
    交易时段：09:25-11:30, 13:00-15:00（周末不交易）
    """
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    minutes = now.hour * 60 + now.minute
    # 9:25=565, 11:30=690, 13:00=780, 15:00=900
    if 565 <= minutes <= 690:
        return True
    if 780 <= minutes <= 900:
        return True
    return False


# ============================================================
# 后台数据拉取线程
# ============================================================
class QuoteWorker(QThread):
    quote_updated = Signal(dict)
    alert_triggered = Signal(dict)
    status_changed = Signal(str)
    batch_done = Signal(str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.running = False
        self._lock = threading.Lock()
        self.alert_cooldown = {}
        self.kline_cache = {}       # {code: (timestamp, klines)} 1分钟; {code_daily/weekly: ...} 日K/周K
        self._kline_lock = threading.Lock()  # 保护 kline_cache 跨线程读写
        self._kline_fetching = False
        self._us_kline_fetching = False   # 美股 1 分钟 K 线预取标志（与 A 股互不阻塞）
        self._kline_period_fetching = False
        self._first_loop = True     # 首次循环标记（启动时无论是否交易时间都请求一次，避免空白）

    def update_config(self, config):
        with self._lock:
            self.config = config

    def _kline_get(self, key):
        """线程安全地读 K 线缓存（返回 (timestamp, klines) 或 None）"""
        with self._kline_lock:
            return self.kline_cache.get(key)

    def _kline_put(self, key, klines):
        """线程安全地写 K 线缓存，并做无界增长防护（超过上限淘汰最久未访问）"""
        with self._kline_lock:
            self.kline_cache[key] = (time.time(), klines)
            if len(self.kline_cache) > 600:
                oldest = min(self.kline_cache, key=lambda k: self.kline_cache[k][0])
                self.kline_cache.pop(oldest, None)

    def stop(self):
        self.running = False

    def run(self):
        """QThread 入口：任何一轮取数抛出的异常都不能让线程静默死掉。

        原实现里 run() 整段没有兜底，异常会让线程直接终止，而 'stopped' 也发不出去
        —— 状态点保持绿色、时间标签停走，用户以为一切正常（本项目最难查的那类故障）。
        """
        self.running = True
        self.status_changed.emit('running')
        fatal_streak = 0
        try:
            while self.running:
                loop_started = time.time()
                try:
                    self._run_loop()
                    break                       # stop() 置位后的正常退出
                except Exception as e:
                    # 能撑过 2 分钟说明是偶发，重新计数；连续快速失败才是真坏了
                    fatal_streak = 1 if time.time() - loop_started > 120 else fatal_streak + 1
                    print(f'[Worker] 取数循环异常（第 {fatal_streak} 次）: {e}')
                    if fatal_streak >= 3:
                        break
                    time.sleep(1)
        finally:
            self.running = False
            self.status_changed.emit('stopped' if fatal_streak < 3 else 'error')
            try:
                close_tdx_client()
            except Exception:
                pass

    def _run_loop(self):
        consecutive_errors = 0
        max_errors = 10

        while self.running:
            with self._lock:
                config = self.config
            interval = max(config.get('interval', 5), 1)
            source = config.get('source', 'auto')
            alerts = config.get('alerts', [])
            loop_success = False
            now_str = datetime.datetime.now().strftime('%H:%M:%S')
            is_first_loop = self._first_loop

            # 批量获取行情（按数据源分组，减少请求次数）
            fetch_start = time.time()
            # 「启用美股」关掉后彻底不碰美股（REST 也不发）：原先只是停 WS，
            # 快照与日K 预取照旧打 Alpaca，既耗配额又和界面开关的语义不符
            us_enabled = bool((config.get('alpaca') or {}).get('enabled'))
            valid_codes = [item.get('code', '') for item in alerts
                           if item.get('code')
                           and (us_enabled or not is_us_symbol(item.get('code')))]

            # 按市场分流：非美股走 A 股五路轮询，美股走 Alpaca（WS 缓存优先，REST 兜底）
            # 美股按归一化代码请求（大写、剥掉 us 前缀），结果再映射回配置里的原样代码：
            # 否则手改/旧版配置的 'aapl'、'usAAPL' 永远查不到键，卡片会一直显示 '--'
            a_codes = [c for c in valid_codes if not is_us_symbol(c)]
            us_key = {}
            for c in valid_codes:
                us_sym = normalize_us_symbol(c)
                if us_sym:
                    us_key.setdefault(us_sym, c)
            us_codes = list(us_key.values())

            results = {}
            # A股交易时间外不刷新行情（首次启动除外，避免空白）
            if a_codes and (is_first_loop or is_a_share_trading_time()):
                results.update(get_quotes_batch(a_codes, source))
            if us_key:
                # 美股每轮都取：交易时段读 WS 内存缓存（零网络开销），
                # 休市时段走 REST，REST 内部有 60s TTL 节流，不会浪费免费版配额
                try:
                    got = alpaca_stream.get_quotes_batch(list(us_key))
                    results.update({us_key[k]: v for k, v in got.items() if k in us_key})
                except Exception as e:
                    print(f'[Alpaca] 美股取数异常: {e}')
            # 休市（A股收盘且列表里没有美股）不是故障：不能计入连续错误，
            # 否则纯 A 股列表每晚都会误报红点，还反复重建 TDX 连接
            if not us_codes and not (a_codes and (is_first_loop or is_a_share_trading_time())):
                loop_success = True
            self._first_loop = False

            # 串行处理结果
            for item in alerts:
                if not self.running:
                    break
                code = item.get('code', '')
                target = item.get('target')
                direction = item.get('dir', 'below')
                target_pct = item.get('target_pct')
                pct_dir = item.get('pct_dir', 'below')
                name = item.get('name', code)
                position = item.get('position', '')
                note = item.get('note', '')

                q = results.get(code)
                if q is None:
                    continue
                price = q.get('price')
                if price is None:
                    continue

                loop_success = True

                # 部分接口会在行情里附带 K 线，直接预填缓存供悬停即时显示
                klines = q.get('klines', [])
                if klines:
                    self._kline_put(code, klines)

                payload = {
                    'name': name,
                    'code': code,
                    'price': price,
                    'change_pct': q.get('change_pct'),
                    'yest_close': q.get('yest_close'),
                    'target': target,
                    'direction': direction,
                    'target_pct': target_pct,
                    'pct_dir': pct_dir,
                    'position': position,
                    'note': note,
                    'last_update': now_str,
                    'klines': klines,
                }
                self.quote_updated.emit(payload)

                if target is not None:
                    triggered, reason = self._check_alert(
                        name, code, price, target, direction
                    )
                    if triggered:
                        cooldown_key = f'{code}_{direction}'
                        last = self.alert_cooldown.get(cooldown_key, 0)
                        if time.time() - last >= 60:
                            self.alert_triggered.emit({
                                'time': now_str, 'message': reason,
                                'code': code, 'name': name,
                                'price': price, 'target': target,
                                'direction': direction,
                            })
                            self.alert_cooldown[cooldown_key] = time.time()

                if target_pct is not None:
                    change_pct = q.get('change_pct')
                    if change_pct is not None:
                        pct_trig, pct_reason = self._check_pct_alert(
                            name, code, change_pct, target_pct, pct_dir
                        )
                        if pct_trig:
                            cooldown_key = f'{code}_pct_{pct_dir}'
                            last = self.alert_cooldown.get(cooldown_key, 0)
                            if time.time() - last >= 60:
                                self.alert_triggered.emit({
                                    'time': now_str, 'message': pct_reason,
                                    'code': code, 'name': name,
                                    'price': price, 'target': target_pct,
                                    'direction': pct_dir,
                                })
                                self.alert_cooldown[cooldown_key] = time.time()

            if loop_success:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    self.status_changed.emit('error')
                    try:
                        close_tdx_client()
                    except Exception:
                        pass
                    consecutive_errors = 0

            self.batch_done.emit(now_str)

            # A-share kline async prefetch (every 30s, non-blocking)
            # 非交易时间跳过1分钟K线刷新（首次启动除外，避免空白）
            if not self._kline_fetching and (is_first_loop or is_a_share_trading_time()):
                self._kline_fetching = True
                codes_to_fetch = []
                now_ts = time.time()
                for c in a_codes:  # 仅 A 股（美股走下方独立预取，门控不同）
                    last_ts = (self._kline_get(c) or (0, []))[0]
                    if now_ts - last_ts > 30:
                        codes_to_fetch.append(c)
                if codes_to_fetch:
                    t = threading.Thread(target=self._fetch_a_share_klines, args=(codes_to_fetch,), daemon=True)
                    t.start()
                else:
                    self._kline_fetching = False

            # 美股1分钟K线异步预取（Alpaca 数据窗口 ET 04:00-20:00 内每 30s 刷新；
            # 休市时数据不变，不重复拉取，悬浮图直接用既有缓存）
            if us_codes and not self._us_kline_fetching \
                    and (is_first_loop or alpaca_history.in_us_data_window()):
                codes_to_fetch = []
                now_ts = time.time()
                for c in us_codes:
                    last_ts = (self._kline_get(c) or (0, []))[0]
                    if now_ts - last_ts > 30:
                        codes_to_fetch.append(c)
                if codes_to_fetch:
                    self._us_kline_fetching = True
                    t = threading.Thread(target=self._fetch_us_klines, args=(codes_to_fetch,), daemon=True)
                    t.start()

            # 日K/周K 异步预加载（启动时全刷；之后距上次预加载 > 60min 即刷新）
            # 不再以「是否交易时间」为硬性门控：交易时间内按此门控正常刷新，
            # 非交易时间若距上次预加载已超 60min 也会刷新，避免长期挂机却一直显示旧 K 线
            if not self._kline_period_fetching:
                period_pairs = []
                now_ts = time.time()
                for c in valid_codes:          # 已按「启用美股」过滤，关掉的不再预取
                    for period in ('daily', 'weekly'):
                        cache_key = f'{c}_{period}'
                        last_ts = (self._kline_get(cache_key) or (0, []))[0]
                        if is_first_loop or now_ts - last_ts > 3600:  # 首轮或距上次预加载 > 60min
                            period_pairs.append((c, period))
                if period_pairs:
                    self._kline_period_fetching = True
                    t = threading.Thread(target=self._fetch_kline_period, args=(period_pairs,), daemon=True)
                    t.start()

            # 等待下一轮（减去本轮获取耗时，保证实际刷新频率）
            fetch_duration = max(0, time.time() - fetch_start)
            sleep_time = max(0.5, interval - fetch_duration)
            slept = 0
            while slept < sleep_time and self.running:
                time.sleep(0.1)
                slept += 0.1

    def _fetch_a_share_klines(self, codes):
        """后台线程: 获取A股1分钟K线并缓存"""
        try:
            for code in codes:
                if not self.running:
                    break
                try:
                    # 分时全天化：拉当天全部1分钟K线（A股240根+缓冲），
                    # latest_day_only 按最后一根日期过滤出同一交易日，
                    # 得到"开盘至当前"的完整数据，而不是只看最近60根
                    klines = get_kline_1min(code, 300, latest_day_only=True)
                    if klines:
                        self._kline_put(code, klines)
                except Exception as e:
                    print(f'[Kline Prefetch] {code} failed: {e}')
        finally:
            self._kline_fetching = False

    def _fetch_us_klines(self, codes):
        """后台线程: 获取美股1分钟K线并缓存（Alpaca REST，feed=iex）"""
        try:
            for code in codes:
                if not self.running:
                    break
                try:
                    # 缓存键用配置里的原样代码（卡片按它取），请求用归一化后的代码
                    klines = alpaca_history.get_kline(normalize_us_symbol(code) or code,
                                                      '1min', 60)
                    if klines:
                        self._kline_put(code, klines)
                except Exception as e:
                    print(f'[US Kline Prefetch] {code} failed: {e}')
        finally:
            self._us_kline_fetching = False

    def _fetch_kline_period(self, code_period_pairs):
        """后台线程: 获取日K/周K并缓存（启动时+每小时刷新），按市场分流"""
        try:
            from price_alert import get_kline
            for code, period in code_period_pairs:
                if not self.running:
                    break
                try:
                    # 美股走 Alpaca，A股走东方财富/腾讯；日K预取顺带更新美股昨收缓存
                    if is_us_symbol(code):
                        klines = alpaca_history.get_kline(code, period, 60)
                    else:
                        klines = get_kline(code, period, 60)
                    if klines:
                        cache_key = f'{code}_{period}'
                        self._kline_put(cache_key, klines)
                except Exception as e:
                    print(f'[Kline {period}] {code} failed: {e}')
        finally:
            self._kline_period_fetching = False

    def _check_alert(self, name, code, price, target, direction):
        # 判定口径与 QuoteCard 共用模块级 check_price_alert，避免两处分叉
        hit, dir_cn = check_price_alert(price, target, direction)
        if not hit:
            return False, ''
        return True, f'{name} {dir_cn} {target} | 当前: {price:.2f}'

    def _check_pct_alert(self, name, code, change_pct, target_pct, pct_dir):
        """涨跌幅预警：判定统一走模块级 check_pct_alert

        写 3 表示 3%，方向由 pct_dir 决定（below=仅跌, above=仅涨, both=涨跌任一）
        """
        hit, direction = check_pct_alert(change_pct, target_pct, pct_dir)
        if not hit:
            return False, ''
        return True, f'{name} {direction}幅 {abs(change_pct):.2f}% 达到 {abs(target_pct)}%'



# ============================================================
# K线悬浮图（鼠标悬停显示1分钟K线）
# ============================================================
KLINE_SIZES = {
    'small':  (320, 180),   # 小（默认）
    'medium': (640, 360),   # 中（长宽各×2）
    'large':  (960, 540),   # 大（长宽各×3）
}


# ============================================================
# 屏幕颜色采样检测器
# ============================================================
class _ScreenColorDetector:
    """采样软件窗口周围桌面区域的像素亮度，判断深浅"""

    def __init__(self, window):
        self.window = window

    def detect(self):
        """返回 'dark' 或 'light'"""
        try:
            import random
            screen = self.window.screen()
            if not screen:
                return 'dark'
            geo = screen.geometry()
            pixmap = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
            img = pixmap.toImage()
            win_geo = self.window.geometry()
            samples = []
            for _ in range(200):
                side = random.choice(['top', 'bottom', 'left', 'right'])
                if side == 'top':
                    x = random.randint(win_geo.left(), max(win_geo.left(), win_geo.right()))
                    y = random.randint(max(0, win_geo.top() - 30), max(0, win_geo.top() - 5))
                elif side == 'bottom':
                    x = random.randint(win_geo.left(), max(win_geo.left(), win_geo.right()))
                    y = random.randint(win_geo.bottom() + 5, min(geo.height(), win_geo.bottom() + 30))
                elif side == 'left':
                    x = random.randint(max(0, win_geo.left() - 30), max(0, win_geo.left() - 5))
                    y = random.randint(win_geo.top(), max(win_geo.top(), win_geo.bottom()))
                else:
                    x = random.randint(win_geo.right() + 5, min(geo.width(), win_geo.right() + 30))
                    y = random.randint(win_geo.top(), max(win_geo.top(), win_geo.bottom()))
                if 0 <= x < img.width() and 0 <= y < img.height():
                    pixel = img.pixelColor(x, y)
                    samples.append(pixel.lightness())
            if not samples:
                return 'dark'
            avg = sum(samples) / len(samples)
            return 'light' if avg > 128 else 'dark'
        except Exception:
            return 'dark'


# ============================================================
# K线悬浮图
# ============================================================
class KlineTooltip(QWidget):
    """K线悬浮图窗口（无边框、置顶、可接收鼠标）

    鼠标交互（为什么不再穿透）：
      图为大尺寸时会大面积悬在悬浮窗外的桌面上，穿透状态下用户对着图
      双击时点击落到桌面，应用毫无反应 —— 即"双击关不掉"的根源。
      改为可接收鼠标后：
        - 双击图 = 双击悬浮窗，关闭整个应用（走主窗口 closeEvent 完整清理）
        - 鼠标移出图 → 自动隐藏（与卡片悬停行为衔接）
        - 单击不做任何事（不抢焦点，不改变显示状态）
    """

    def __init__(self, size_key='small', main_window=None):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus   # 点击图时不抢主窗口焦点
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 主窗口引用用于双击关闭；下划线前缀，避免被外部遍历
        self._main_window = main_window
        w, h = KLINE_SIZES.get(size_key, KLINE_SIZES['small'])
        self.setFixedSize(w, h)

        self.code = ''
        self.name = ''
        self.klines = []

    def mouseDoubleClickEvent(self, event):
        """双击K线图 = 关闭整个应用（与双击悬浮窗本体等效）"""
        if event.button() == Qt.LeftButton and self._main_window is not None:
            event.accept()
            self._main_window.close()
        else:
            super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        """鼠标离开K线图: 自动隐藏"""
        self.hide()
        super().leaveEvent(event)

    def set_data(self, code, name, klines):
        self.code = code
        self.name = name
        self.klines = klines
        self.update()

    def _price_fmt(self, price):
        """根据代码返回格式化后的价格字符串（ETF/基金3位小数，其他2位）"""
        code = self.code.lower()
        if code.startswith(('sh51', 'sz15', 'sz16', 'sh50', 'sh58')):
            return f'{price:.3f}'
        return f'{price:.2f}'

    @property
    def size_key(self):
        w, h = self.width(), self.height()
        for key, (sw, sh) in KLINE_SIZES.items():
            if w == sw and h == sh:
                return key
        return 'small' 

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 背景
        bg_color = QColor(get_color('bg'))
        bg_color.setAlpha(245)
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(QColor(get_color('border')), 1))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 8, 8)

        if not self.klines:
            p.setPen(QColor(get_color('text_dim')))
            p.setFont(QFont('Microsoft YaHei UI', 9))
            p.drawText(self.rect(), Qt.AlignCenter, '暂无K线数据')
            p.end()
            return

        # 标题: 名称 + 代码 + 最新价
        last = self.klines[-1]
        first = self.klines[0]
        p.setPen(QColor(get_color('text')))
        p.setFont(QFont('Microsoft YaHei UI', 9, QFont.Bold))
        title = f'{self.name}  {self.code}  {self._price_fmt(last["close"])}'
        p.drawText(10, 16, title)

        # 涨跌幅（除的是首根开盘价，守卫也必须看它：Alpaca 缺字段时 open 会是 0）
        if first['open'] > 0:
            pct = ((last['close'] - first['open']) / first['open']) * 100
        else:
            pct = 0
        pct_color = QColor(COLOR_UP) if pct >= 0 else QColor(COLOR_DOWN)
        pct_str = f'{pct:+.2f}%'
        p.setPen(pct_color)
        fm = p.fontMetrics()
        title_w = fm.horizontalAdvance(title)
        p.drawText(10 + title_w + 8, 16, pct_str)

        # K线绘制区域
        x0, y0 = 10, 26
        w = self.width() - 20
        h = self.height() - 42

        n = len(self.klines)
        # 浮点步长全量压缩：分时已是当天全量数据(~240根)，原先固定
        # 2px蜡烛+1px间隔在 small(320px) 宽度下只能画 ~100 根，画不下全天。
        # 改为步长 = 绘制区宽 / n 自适应，蜡烛宽度最小压到 1px；
        # 少量数据（日K/周K 60根）时步长变大，蜡烛自动变宽，行为与原先一致
        step = w / n if n else 1.0
        bar_w = max(1, int(step) - 1)

        # 价格范围
        max_price = max(k['high'] for k in self.klines)
        min_price = min(k['low'] for k in self.klines)
        price_range = max_price - min_price
        if price_range == 0:
            price_range = max(0.01, abs(max_price) * 0.001)

        def price_to_y(price):
            return y0 + h - (price - min_price) / price_range * h

        # 画K线
        for i, k in enumerate(self.klines):
            x = x0 + int(i * step)          # 浮点步长取整落位，保证最后
            open_y = price_to_y(k['open'])   # 一根不越出右边界
            close_y = price_to_y(k['close'])
            high_y = price_to_y(k['high'])
            low_y = price_to_y(k['low'])

            if k['close'] >= k['open']:
                color = QColor(COLOR_UP)   # 红涨
            else:
                color = QColor(COLOR_DOWN)  # 绿跌

            p.setPen(QPen(color, 1))
            # 影线
            cx = x + bar_w // 2
            p.drawLine(cx, int(high_y), cx, int(low_y))
            # 实体
            body_top = int(min(open_y, close_y))
            body_h = max(1, int(abs(close_y - open_y)))
            p.setBrush(QBrush(color))
            p.drawRect(x, body_top, bar_w, body_h)

        # 价格标注
        p.setPen(QColor(get_color('text_dim')))
        p.setFont(QFont('Microsoft YaHei UI', 7))
        p.drawText(x0 + 2, y0 + 8, self._price_fmt(max_price))
        p.drawText(x0 + 2, y0 + h, self._price_fmt(min_price))

        # 时间标注
        if n > 0:
            p.drawText(x0, self.height() - 6, self.klines[0]['time'])
            last_time = self.klines[-1]['time']
            tw = p.fontMetrics().horizontalAdvance(last_time)
            p.drawText(self.width() - 10 - tw, self.height() - 6, last_time)

        p.end()


# ============================================================
# 行情卡片 Widget（1/3 宽度紧凑版）
# ============================================================
class QuoteCard(QFrame):
    """紧凑行情卡片（窗口宽度 1/3）

    颜色规则：
      - 默认：深色背景
      - 触发预警（目标价 or 涨跌幅）：绿色稳定显示
    """

    def __init__(self, code, name, parent=None):
        super().__init__(parent)
        self.code = code
        self.name = name
        self._triggered = False

        self.setObjectName('quoteCard')
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(4)

        # 名称（截断显示 + 悬停显示完整）
        self.name_label = QLabel(name)
        self.name_label.setFixedWidth(70)
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.name_label.setStyleSheet(
            f'color: {get_color("text")}; font-size: 10px; font-weight: 600;'
        )
        self._update_name_elided()
        layout.addWidget(self.name_label)

        # 市场标签
        self.market_label = QLabel('')
        self.market_label.setFixedWidth(16)
        self.market_label.setAlignment(Qt.AlignCenter)
        self.market_label.setStyleSheet(
            f'color: {get_color("text_dim")}; font-size: 8px;'
            f'border: 1px solid {get_color("border")}; border-radius: 2px;'
        )
        layout.addWidget(self.market_label)

        # 价格（固定宽度 + 右对齐）
        self.price_label = QLabel('--')
        self.price_label.setFixedWidth(70)
        self.price_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.price_label.setStyleSheet(
            f'color: {get_color("text")}; font-size: 12px; font-weight: 700;'
        )
        layout.addWidget(self.price_label)

        # 涨跌幅（固定宽度 + 右对齐）
        self.change_label = QLabel('')
        self.change_label.setFixedWidth(52)
        self.change_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.change_label.setStyleSheet(
            f'color: {COLOR_FLAT}; font-size: 10px; font-weight: 600;'
        )
        layout.addWidget(self.change_label)

        # 状态/触发信息（弹性）
        self.status_label = QLabel('')
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        layout.addWidget(self.status_label, 1)

        # 更新时间
        self.time_label = QLabel('')
        self.time_label.setFixedWidth(48)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 8px;')
        layout.addWidget(self.time_label)

        self._apply_default_style()

        # K线悬浮图: hover防抖定时器（用QTimer实例，便于stop）
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._request_kline)
        self.setMouseTracking(True)
        # 不在构造时常驻开启 WA_Hover（持续产生 hover 事件有开销），
        # 改为进入卡片时开启、离开时关闭（见 enterEvent/leaveEvent）。
        # 周期按钮自身的 WA_Hover 保留，用于 hover 切换 K 线周期。

        # K线周期切换按钮（浮动在卡片右侧内容上方，不占布局空间）
        self._period_buttons = {}
        self._period_active = '1min'
        for period, text in [('1min', '分时'), ('daily', '日K'), ('weekly', '周K')]:
            btn = QLabel(text, self)
            btn.setFixedHeight(14)
            btn.setFixedWidth(28)
            btn.setAlignment(Qt.AlignCenter)
            btn.setStyleSheet(self._period_btn_style(period == '1min'))
            btn.setAttribute(Qt.WA_Hover, True)
            btn.setMouseTracking(True)
            btn.installEventFilter(self)
            btn.setVisible(False)
            self._period_buttons[period] = btn

    def _period_btn_style(self, active):
        """周期按钮样式: active=当前选中(高亮) / inactive=普通"""
        if active:
            return (f'color: {COLOR_DOWN}; font-size: 8px; font-weight: 600;'
                    f'background: {get_color("border")};'
                    f'border: 1px solid {COLOR_DOWN}; border-radius: 2px;')
        return (f'color: {get_color("text")}; font-size: 8px;'
                f'background: {get_color("bg_light")};'
                f'border: 1px solid {get_color("border")}; border-radius: 2px;')

    def _show_period_buttons(self):
        """定位并显示周期切换按钮（浮在卡片左侧名称之后，不额外占地方）"""
        self._period_active = '1min'
        # 起始位置：名称(70) + 市场(16) + 边距(4+4) 之后
        x = 4 + 70 + 4 + 16 + 4
        for period in ['1min', 'daily', 'weekly']:
            btn = self._period_buttons[period]
            btn.setStyleSheet(self._period_btn_style(period == '1min'))
            btn.move(x, 5)
            btn.raise_()
            btn.setVisible(True)
            x += 30

    def enterEvent(self, event):
        """鼠标进入卡片: 300ms防抖后请求K线，并显示周期按钮"""
        # 进入时才开启 WA_Hover，减少非悬停态下的 hover 事件处理开销
        self.setAttribute(Qt.WA_Hover, True)
        self._hover_timer.start(300)
        self._show_period_buttons()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开卡片: 取消请求并隐藏K线图和按钮

        特例: 光标已移入K线悬浮图矩形内时保持图显示 —— 否则图会在鼠标
        移向它的瞬间随本事件消失，"双击K线图关闭应用"永远点不到。
        鼠标离开图时由 KlineTooltip.leaveEvent 自行隐藏。
        """
        # 离开时关闭 WA_Hover，hover 开销只在悬停期间承担
        self.setAttribute(Qt.WA_Hover, False)
        self._hover_timer.stop()
        for btn in self._period_buttons.values():
            btn.setVisible(False)
        win = self.window()
        tip = getattr(win, '_kline_tooltip', None)
        if tip is not None and tip.isVisible() \
                and tip.frameGeometry().contains(QCursor.pos()):
            pass    # 光标进入K线图区域，图保持显示
        elif hasattr(win, 'hide_kline_tooltip'):
            win.hide_kline_tooltip()
        super().leaveEvent(event)

    def eventFilter(self, obj, event):
        """处理周期按钮的 hover: 指向哪个按钮就切换到对应周期K线"""
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.HoverEnter:
            for period, btn in self._period_buttons.items():
                if obj == btn:
                    self._period_active = period
                    for p, b in self._period_buttons.items():
                        b.setStyleSheet(self._period_btn_style(p == period))
                    self._request_kline_with_period(period)
                    return True
        elif event.type() == QEvent.HoverLeave:
            for btn in self._period_buttons.values():
                if obj == btn:
                    # 离开按钮时保持当前 active 高亮
                    for p, b in self._period_buttons.items():
                        b.setStyleSheet(self._period_btn_style(p == self._period_active))
                    return True
        return super().eventFilter(obj, event)

    def _request_kline(self):
        """请求该股票的分时K线（默认1分钟）"""
        win = self.window()
        if hasattr(win, 'show_kline_for_card'):
            win.show_kline_for_card(self.code, self.name, QCursor.pos(), '1min')

    def _request_kline_with_period(self, period):
        """请求对应周期的K线（从缓存取，零延迟）"""
        win = self.window()
        if hasattr(win, 'show_kline_for_card'):
            win.show_kline_for_card(self.code, self.name, QCursor.pos(), period)

    def _update_name_elided(self):
        """截断长名称显示"""
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.name_label.font())
        elided = fm.elidedText(self.name, Qt.ElideRight, self.name_label.width() - 4)
        self.name_label.setText(elided)

    def _apply_default_style(self):
        self._triggered = False
        self.setStyleSheet(
            f'#quoteCard {{'
            f'  background: {get_color("default_card")};'
            f'  border: 1px solid {get_color("border")};'
            f'  border-radius: 6px;'
            f'}}'
            f'QLabel {{ background: transparent; color: {get_color("text")}; }}'
        )
        self.update()

    def _apply_triggered_style(self):
        self._triggered = True
        # 触发态背景/边框统一由 paintEvent 绘制，这里不再设置背景色与边框，
        # 避免「样式表背景」与「paintEvent 重绘」双重绘制导致覆盖/闪烁。
        # 仅保留标签文字色（黑）的样式。
        self.setStyleSheet(
            f'#quoteCard {{ border: none; }}'
            f'QLabel {{ background: transparent; color: #000000; }}'
        )
        self.update()

    def paintEvent(self, event):
        """重写paintEvent确保触发时整个卡片背景完全覆盖"""
        if self._triggered:
            from PySide6.QtGui import QPainter, QColor, QPen, QBrush
            from PySide6.QtCore import QRectF
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            # 填充背景
            p.setBrush(QBrush(QColor(COLOR_TRIGGERED)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, 6, 6)
            # 白色边框
            p.setPen(QPen(QColor('#ffffff'), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, 6, 6)
            p.end()
        super().paintEvent(event)

    def set_data(self, data):
        price = data.get('price')
        change_pct = data.get('change_pct')
        target = data.get('target')
        direction = data.get('direction', 'below')
        target_pct = data.get('target_pct')
        pct_dir = data.get('pct_dir', 'below')
        position = data.get('position', '')
        note = data.get('note', '')
        last_update = data.get('last_update', '')
        name = data.get('name', self.name)

        # 更新名称（如有变化）
        if name != self.name:
            self.name = name
            self._update_name_elided()

        # 货币符号：A股显示人民币，美股显示美元
        currency_symbol = '$' if is_us_symbol(self.code) else '¥'

        if price is not None:
            # ETF（510开头）和部分基金需要3位小数，其他股票2位
            code = self.code.lower()
            if code.startswith(('sh51', 'sz15', 'sz16', 'sh50', 'sh58')):
                # ETF/基金：3位小数
                self.price_label.setText(f'{currency_symbol}{price:,.3f}')
            else:
                self.price_label.setText(f'{currency_symbol}{price:,.2f}')
        else:
            self.price_label.setText('--')

        # 涨跌幅
        if change_pct is not None:
            color = COLOR_UP if change_pct > 0 else (COLOR_DOWN if change_pct < 0 else COLOR_FLAT)
            sign = '+' if change_pct > 0 else ''
            self.change_label.setText(f'{sign}{change_pct:.2f}%')
            self.change_label.setStyleSheet(
                f'color: {color}; font-size: 10px; font-weight: 600;'
            )
        else:
            self.change_label.setText('')

        # 触发判断：目标价 OR 涨跌幅
        triggered = False
        trigger_reason = ''

        if target is not None and price is not None:
            # 判定口径与 QuoteWorker 共用模块级 check_price_alert，避免两处分叉
            hit, dir_cn = check_price_alert(price, target, direction)
            if hit:
                triggered = True
                # both 方向不展示具体破位方向，沿用原口径「双向」
                label = '双向' if direction == 'both' else dir_cn
                trigger_reason = f'{label}{target}'

        if not triggered and target_pct is not None and change_pct is not None:
            # 判定口径与 QuoteWorker 共用同一函数，避免两处逻辑再次分叉
            pct_hit, direction_cn = check_pct_alert(
                change_pct, target_pct, data.get('pct_dir', 'both')
            )
            if pct_hit:
                triggered = True
                trigger_reason = f'{direction_cn}≥{abs(target_pct)}%'

        # 状态显示：不显示预警文字，仅通过绿色底色表示触发
        self.status_label.setText('')

        # 只在状态变化时切换样式
        if triggered and not self._triggered:
            self._apply_triggered_style()
        elif not triggered and self._triggered:
            self._apply_default_style()

        # 市场标签：A股显示「A」，美股显示「US」
        self.market_label.setText('US' if is_us_symbol(self.code) else 'A')
        self.market_label.setStyleSheet(
            f'color: {get_color("text_dim")}; font-size: 9px;'
            f'border: 1px solid {get_color("border")}; border-radius: 2px;'
        )

    def _on_theme_changed(self):
        """主题切换时刷新所有标签颜色"""
        self.name_label.setStyleSheet(
            f'color: {get_color("text")}; font-size: 10px; font-weight: 600;'
        )
        self.price_label.setStyleSheet(
            f'color: {get_color("text")}; font-size: 12px; font-weight: 700;'
        )
        self.time_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 8px;')
        self.status_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        # 市场标签
        self.market_label.setStyleSheet(
            f'color: {get_color("text_dim")}; font-size: 9px;'
            f'border: 1px solid {get_color("border")}; border-radius: 2px;'
        )
        # 重新应用卡片样式
        if self._triggered:
            self._apply_triggered_style()
        else:
            self._apply_default_style()
        self._update_name_elided()



# ============================================================
# 异步股票代码查询线程（避免网络请求阻塞UI）
# ============================================================
class _StockResolveThread(QThread):
    """在后台线程查询股票代码，避免阻塞UI事件循环"""
    finished_resolve = Signal(list)  # 传递候选列表 [(code, name), ...]

    def __init__(self, input_text):
        super().__init__()
        self.input_text = input_text

    def run(self):
        candidates = []
        try:
            from price_alert import get_quote_tencent
            text = self.input_text.strip()
            lower_text = text.lower()

            # 美股代码（含 usAAPL 写法）：名称从 Alpaca assets 接口解析，失败留空由用户自填
            us_sym = normalize_us_symbol(text)
            if us_sym:
                name = ''
                try:
                    name = alpaca_history.get_symbol_name(us_sym)
                except Exception:
                    name = ''
                candidates = [(us_sym, name)]

            # 已带前缀
            elif lower_text.startswith(('sh', 'sz', 'hk', 'bj')):
                try:
                    q = get_quote_tencent(text)
                    name = q.get('name', '')
                    price = q.get('price')
                    if name and name != text and price and price > 0:
                        candidates = [(text, name)]
                    else:
                        candidates = [(text, '')]
                except Exception:
                    candidates = [(text, '')]

            # 6位数字: sh 和 sz 两边都查
            elif text.isdigit() and len(text) == 6:
                for prefix in ['sh', 'sz']:
                    full_code = prefix + text
                    try:
                        q = get_quote_tencent(full_code)
                        name = q.get('name', '')
                        price = q.get('price')
                        if name and name != full_code and price and price > 0:
                            candidates.append((full_code, name))
                    except Exception:
                        continue
                if not candidates:
                    candidates = [('sh' + text, '')]

        except Exception as e:
            import traceback
            print(f'[StockResolveThread] error: {e}\n{traceback.format_exc()}')

        self.finished_resolve.emit(candidates)


# ============================================================
# Alpaca 凭证弹窗（勾选「启用美股」时才出现，设置页不显示账号信息）
# ============================================================
class AlpacaCredsDialog(QDialog):
    """Alpaca API Key/Secret 输入弹窗

    - 预填本地已存凭证（解密回填；Secret 密文回显下看不到内容，直接点确定即沿用旧凭证）
    - 确定 → accept，调用方把凭证加密写进内存配置（随「保存」落盘）
    - 取消 → reject，调用方回滚勾选状态
    """

    def __init__(self, parent=None, api_key='', secret=''):
        super().__init__(parent)
        self.setWindowTitle('Alpaca 凭证')
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        tip = QLabel(
            '输入 Alpaca API Key / Secret（免费版即可，paper 账户）。\n'
            '凭证经 Windows DPAPI 加密后仅保存在本机配置文件，不会明文落盘。'
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)

        form = QFormLayout()
        self.key_edit = QLineEdit(api_key)
        self.key_edit.setPlaceholderText('Alpaca API Key ID')
        form.addRow('API Key:', self.key_edit)
        self.secret_edit = QLineEdit(secret)
        self.secret_edit.setEchoMode(QLineEdit.Password)
        self.secret_edit.setPlaceholderText('Alpaca Secret Key')
        form.addRow('Secret:', self.secret_edit)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok_btn = QPushButton('确定')
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('取消')
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

    def get_creds(self):
        """返回 (api_key, secret) 的去空格输入值"""
        return self.key_edit.text().strip(), self.secret_edit.text().strip()


# ============================================================
# 设置对话框（增删改监控股票）
# ============================================================
class SettingsDialog(QDialog):
    config_changed = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = json.loads(json.dumps(config))
        self._build_ui()
        self._load_table()
        # 监听代码单元格变化，自动解析名称
        self.table.cellChanged.connect(self._on_code_changed)
        self._block_signals = False  # 防止信号循环
        self._resolve_thread = None  # 异步查询线程引用

    def _build_ui(self):
        self.setWindowTitle('监控设置')
        self.setMinimumSize(720, 520)
        self.setStyleSheet(
            f'QDialog {{ background: {get_color("bg")}; color: {get_color("text")}; }}'
            f'QLabel {{ color: {get_color("text")}; font-size: 12px; }}'
            f'QLineEdit, QComboBox, QSpinBox {{'
            f'  background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  border: 1px solid {get_color("border")}; border-radius: 4px;'
            f'  padding: 4px 6px; font-size: 12px;'
            f'}}'
            f'QLineEdit:focus, QComboBox:focus {{ border-color: {COLOR_DOWN}; }}'
            f'QComboBox QAbstractItemView {{'
            f'  background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  border: 1px solid {get_color("border")};'
            f'  selection-background-color: {get_color("border")}; selection-color: {get_color("text")};'
            f'  outline: none; padding: 2px; font-size: 12px;'
            f'}}'
            f'QComboBox::drop-down {{ border: none; width: 20px; }}'
            f'QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;'
            f'  border-right: 4px solid transparent; border-top: 5px solid {get_color("text_dim")}; }}'
            f'QSpinBox::up-button, QSpinBox::down-button {{'
            f'  background: {get_color("bg_light")}; border: none; width: 16px; }}'
            f'QSpinBox::up-arrow {{ border-left: 3px solid transparent;'
            f'  border-right: 3px solid transparent; border-bottom: 4px solid {get_color("text_dim")}; }}'
            f'QSpinBox::down-arrow {{ border-left: 3px solid transparent;'
            f'  border-right: 3px solid transparent; border-top: 4px solid {get_color("text_dim")}; }}'
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 全局设置
        top_box = QGroupBox('全局设置')
        top_box.setStyleSheet(
            f'QGroupBox {{ color: {get_color("text")}; border: 1px solid {get_color("border")};'
            f'  border-radius: 6px; margin-top: 10px; padding-top: 14px;'
            f'  font-weight: 600; font-size: 12px;'
            f'}}'
            f'QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}'
        )
        top_form = QFormLayout(top_box)
        top_form.setSpacing(8)

        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 120)
        self.interval_spin.setValue(self.config.get('interval', 5))
        self.interval_spin.setSuffix(' 秒')
        top_form.addRow('刷新间隔:', self.interval_spin)

        self.kline_size_combo = QComboBox()
        self.kline_size_combo.addItems(['small', 'medium', 'large'])
        self.kline_size_combo.setCurrentText(self.config.get('kline_size', 'small'))
        top_form.addRow('K线图大小:', self.kline_size_combo)

        self.source_combo = QComboBox()
        self.source_combo.addItems(['auto', 'tencent', 'tdx'])
        self.source_combo.setCurrentText(self.config.get('source', 'auto'))
        top_form.addRow('数据源:', self.source_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(['auto', 'dark', 'light'])
        self.theme_combo.setCurrentText(self.config.get('theme', 'auto'))
        top_form.addRow('主题:', self.theme_combo)
        self.tdx_host_edit = QLineEdit()
        self.tdx_host_edit.setText(self.config.get('tdx_host', ''))
        self.tdx_host_edit.setPlaceholderText('留空使用默认节点')
        top_form.addRow('TDX服务器:', self.tdx_host_edit)

        # Alpaca（美股）配置区：设置页不显示任何账号信息；
        # 勾选启用时才弹窗输入凭证（DPAPI 加密存本地），取消勾选不清除已存凭证。
        # 注意：必须先 setChecked 再连 toggled，否则已启用状态下每次打开设置都会弹窗
        alc_cfg = self.config.get('alpaca') or {}
        self.us_enable_check = QCheckBox('启用美股（夜盘实时 WS + REST 历史K线）')
        self.us_enable_check.setChecked(bool(alc_cfg.get('enabled')))
        self.us_enable_check.toggled.connect(self._on_us_enable_toggled)
        top_form.addRow('美股:', self.us_enable_check)
        layout.addWidget(top_box)

        # 表格
        table_label = QLabel('监控列表（双击编辑）')
        table_label.setStyleSheet('font-weight: 600; font-size: 12px; margin-top: 4px;')
        layout.addWidget(table_label)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            ['代码', '名称', '目标价', '方向', '目标%', '%方向', '仓位', '备注', '操作']
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Fixed)
        self.table.setColumnWidth(8, 60)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.SelectedClicked)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setStyleSheet(
            f'QTableWidget {{ gridline-color: {get_color("border")};'
            f'  background: {get_color("bg")}; alternate-background-color: {get_color("bg_light")};'
            f'  border: 1px solid {get_color("border")}; border-radius: 6px;'
            f'}}'
            f'QTableWidget::item {{ padding: 4px; }}'
            f'QHeaderView::section {{'
            f'  background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  padding: 4px; border: none; border-right: 1px solid {get_color("border")};'
            f'  font-weight: 600;'
            f'}}'
        )
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

        btn_add_row = QPushButton('+ 新增')
        btn_add_row.setCursor(Qt.PointingHandCursor)
        btn_add_row.setStyleSheet(
            f'QPushButton {{ background: {COLOR_DOWN}; color: white; border: none;'
            f'  padding: 6px 16px; border-radius: 4px; font-size: 12px; font-weight: 600;'
            f'}}'
            f'QPushButton:hover {{ opacity: 0.85; }}'
        )
        btn_add_row.clicked.connect(self._add_row)
        layout.addWidget(btn_add_row)

        bottom = QHBoxLayout()
        bottom.addStretch()

        btn_cancel = QPushButton('取消')
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_cancel.setStyleSheet(
            f'QPushButton {{ background: {COLOR_FLAT}; color: white; border: none;'
            f'  padding: 6px 20px; border-radius: 4px; font-size: 12px;'
            f'}}'
            f'QPushButton:hover {{ opacity: 0.85; }}'
        )
        btn_cancel.clicked.connect(self.reject)

        btn_save = QPushButton('保存')
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet(
            f'QPushButton {{ background: {COLOR_DOWN}; color: white; border: none;'
            f'  padding: 6px 20px; border-radius: 4px; font-size: 12px; font-weight: 600;'
            f'}}'
            f'QPushButton:hover {{ opacity: 0.85; }}'
        )
        btn_save.clicked.connect(self._save_and_close)

        bottom.addWidget(btn_cancel)
        bottom.addWidget(btn_save)
        layout.addLayout(bottom)

    def _load_table(self):
        self._loading = True
        alerts = self.config.get('alerts', [])
        self.table.setRowCount(len(alerts))
        dir_items = ['below', 'above', 'both']
        dir_labels = [DIR_LABELS[v] for v in dir_items]
        pct_dir_items = ['below', 'above', 'both']
        pct_dir_labels = [PCT_DIR_LABELS[v] for v in pct_dir_items]
        pos_items = ['', '多仓', '空仓']

        for row, item in enumerate(alerts):
            self._set_cell(row, 0, item.get('code', ''))
            self._set_cell(row, 1, item.get('name', ''))
            target = item.get('target')
            self._set_cell(row, 2, str(target) if target is not None else '')
            self._set_combo_cell(row, 3, item.get('dir', 'below'), dir_items, dir_labels)
            target_pct = item.get('target_pct')
            self._set_cell(row, 4, str(target_pct) if target_pct is not None else '')
            self._set_combo_cell(row, 5, item.get('pct_dir', 'both'), pct_dir_items, pct_dir_labels)
            self._set_combo_cell(row, 6, item.get('position', ''), pos_items)
            self._set_cell(row, 7, item.get('note', ''))

            del_btn = QPushButton('✕')
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setFixedSize(28, 24)
            del_btn.setStyleSheet(
                f'QPushButton {{ background: transparent; color: {COLOR_ALERT};'
                f'  border: 1px solid {COLOR_ALERT}; border-radius: 3px; font-size: 12px;'
                f'}}'
                f'QPushButton:hover {{ background: {COLOR_ALERT}; color: white; }}'
            )
            del_btn.clicked.connect(lambda _=False, b=del_btn: self._delete_row(b))
            self.table.setCellWidget(row, 8, del_btn)
        self._loading = False

    def _set_cell(self, row, col, value):
        item = QTableWidgetItem(str(value))
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor(get_color('text')))
        self.table.setItem(row, col, item)

    def _set_combo_cell(self, row, col, value, items, labels=None):
        """设置下拉单元格，labels为中文显示映射"""
        combo = QComboBox()
        if labels:
            # 中文显示，英文存储
            for label_item, data_val in zip(labels, items):
                combo.addItem(label_item, data_val)
            # 选中匹配的英文值
            idx = items.index(value) if value in items else 0
            combo.setCurrentIndex(idx)
        else:
            combo.addItems(items)
            combo.setCurrentText(value if value in items else items[0])
        combo.setStyleSheet(
            f'QComboBox {{ background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  border: 1px solid {get_color("border")}; border-radius: 3px; padding: 2px 4px;'
            f'}}'
        )
        self.table.setCellWidget(row, col, combo)

    def _add_row(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        dir_items = ['below', 'above', 'both']
        dir_labels = [DIR_LABELS[v] for v in dir_items]
        pct_dir_items = ['below', 'above', 'both']
        pct_dir_labels = [PCT_DIR_LABELS[v] for v in pct_dir_items]
        pos_items = ['', '多仓', '空仓']

        self._set_cell(row, 0, '')
        self._set_cell(row, 1, '')
        self._set_cell(row, 2, '')
        self._set_combo_cell(row, 3, 'below', dir_items, dir_labels)
        self._set_cell(row, 4, '')
        self._set_combo_cell(row, 5, 'both', pct_dir_items, pct_dir_labels)
        self._set_combo_cell(row, 6, '', pos_items)
        self._set_cell(row, 7, '')

        del_btn = QPushButton('✕')
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFixedSize(28, 24)
        del_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {COLOR_ALERT};'
            f'  border: 1px solid {COLOR_ALERT}; border-radius: 3px; font-size: 12px;'
            f'}}'
            f'QPushButton:hover {{ background: {COLOR_ALERT}; color: white; }}'
        )
        del_btn.clicked.connect(lambda _=False, b=del_btn: self._delete_row(b))
        self.table.setCellWidget(row, 8, del_btn)
        self.table.setCurrentCell(row, 0)

    def _delete_row(self, btn):
        """按按钮「当前」所在行删除。

        不能用创建时捕获的行号：removeRow 会把下方行整体上移，
        旧行号继续用就会删错行（保存即真丢自选股）。
        """
        for row in range(self.table.rowCount()):
            if self.table.cellWidget(row, 8) is btn:
                self.table.removeRow(row)
                return

    def _on_code_changed(self, row, col):
        """代码单元格变化时,启动异步线程搜索名称(避免阻塞UI)"""
        if self._block_signals or getattr(self, '_loading', False):
            return
        if col != 0:  # 只处理代码列
            return

        item = self.table.item(row, col)
        if not item:
            return
        code = item.text().strip()
        if not code:
            return

        # 已带前缀的不再重复查询（美股不带前缀，需走解析线程补名称）
        if code.lower().startswith(('sh', 'sz', 'hk', 'bj')):
            return

        name_item = self.table.item(row, 1)

        # 回调必须绑定「发出信号的那个线程」：原先是从 self._resolve_thread 反查 item，
        # 旧线程已结束但排队中的回调会把候选写进新线程的行 —— 名称/代码错挂到别的股票。
        # 绑定后既不用 wait 阻塞界面，也不用担心误删引用（运行中的 QThread 被 GC 会 abort）。
        th = _StockResolveThread(code)
        th._row = row
        th._item = item
        th._name_item = name_item
        th.finished_resolve.connect(lambda cands, t=th: self._on_resolve_done(cands, t))
        self._resolve_thread = th
        # 保住仍在跑的线程引用（PySide6 不会替运行中的 QThread 兜住 Python 引用）
        self._resolve_threads = [t for t in getattr(self, '_resolve_threads', []) if t.isRunning()]
        self._resolve_threads.append(th)
        th.start()

    def _on_resolve_done(self, candidates, thread):
        """异步查询完成回调(在主线程执行)；只写发起这次查询的那一行"""
        if not candidates:
            return

        item = getattr(thread, '_item', None)
        name_item = getattr(thread, '_name_item', None)
        if item is None:
            return

        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            # 多个候选，弹窗选择
            labels = [f'{c[0]}  {c[1]}' for c in candidates]
            from PySide6.QtWidgets import QInputDialog
            sel, ok = QInputDialog.getItem(
                self, '选择股票',
                f'匹配到以下 {len(candidates)} 只股票，请选择：',
                labels, 0, False
            )
            if ok and sel:
                chosen = candidates[labels.index(sel)]

        if chosen:
            self._block_signals = True
            item.setText(chosen[0])
            if name_item:
                name_item.setText(chosen[1] or '')
            self._block_signals = False

    def _on_us_enable_toggled(self, checked):
        """「启用美股」勾选联动

        - 勾选：弹凭证窗，预填本地已存凭证（解密回填）；确定 → 加密写入内存配置，
          随「保存」落盘；取消/留空 → 回滚勾选为未启用
        - 取消勾选：什么都不做（凭证保留，下次再勾选弹窗仍预填旧值可继续用）
        """
        if not checked:
            return
        alc = self.config.get('alpaca')
        if not isinstance(alc, dict):
            alc = {}
            self.config['alpaca'] = alc
        key, secret = secret_store.load_alpaca_creds(alc)
        dlg = AlpacaCredsDialog(self, key, secret)
        if dlg.exec() == QDialog.Accepted:
            k, s = dlg.get_creds()
            if not k or not s:
                QMessageBox.warning(self, '凭证不完整', 'API Key 和 Secret 都不能为空。')
                self._set_us_check_silent(False)
                return
            # DPAPI 加密写入内存段（点「保存」时随其它设置一起写盘）
            secret_store.store_alpaca_creds(alc, k, s)
        else:
            self._set_us_check_silent(False)

    def _set_us_check_silent(self, checked):
        """回滚复选框状态但不触发 toggled（避免再次弹窗）"""
        self.us_enable_check.blockSignals(True)
        self.us_enable_check.setChecked(checked)
        self.us_enable_check.blockSignals(False)

    def _save_and_close(self):
        alerts = []
        for row in range(self.table.rowCount()):
            code_item = self.table.item(row, 0)
            name_item = self.table.item(row, 1)
            target_item = self.table.item(row, 2)
            dir_combo = self.table.cellWidget(row, 3)
            pct_target_item = self.table.item(row, 4)
            pct_dir_combo = self.table.cellWidget(row, 5)
            pos_combo = self.table.cellWidget(row, 6)
            note_item = self.table.item(row, 7)

            code = code_item.text().strip() if code_item else ''
            if not code:
                continue
            # 保存时校验代码格式（A股 或 美股），避免脏代码入库（取数时会被静默忽略）
            c_low = code.lower()
            is_a_format = (c_low[:2] in ('sh', 'sz', 'bj') and c_low[2:].isdigit() and len(c_low) == 8)
            if is_a_format:
                pass
            elif is_us_symbol(code):
                code = normalize_us_symbol(code)   # 美股统一存大写（AAPL / BRK.B）
            else:
                QMessageBox.warning(
                    self, '代码格式错误',
                    f'「{code}」不是合法代码（A股: sh/sz/bj + 6位数字；美股: 1-5位字母如 AAPL），已跳过',
                )
                continue

            name = name_item.text().strip() if name_item else code

            target_text = target_item.text().strip() if target_item else ''
            target = None
            if target_text:
                try:
                    target = float(target_text)
                except ValueError:
                    target = None

            target_pct_text = pct_target_item.text().strip() if pct_target_item else ''
            target_pct = None
            if target_pct_text:
                try:
                    target_pct = float(target_pct_text)
                except ValueError:
                    target_pct = None

            direction = dir_combo.currentData() if dir_combo and dir_combo.currentData() else 'below'
            pct_dir = pct_dir_combo.currentData() if pct_dir_combo and pct_dir_combo.currentData() else 'both'
            position = pos_combo.currentText() if pos_combo else ''
            note = note_item.text().strip() if note_item else ''

            alerts.append({
                'code': code,
                'name': name,
                'target': target,
                'dir': direction,
                'target_pct': target_pct,
                'pct_dir': pct_dir,
                'position': position,
                'note': note,
            })

        self.config['interval'] = self.interval_spin.value()
        self.config['kline_size'] = self.kline_size_combo.currentText()
        self.config['source'] = self.source_combo.currentText()
        self.config['theme'] = self.theme_combo.currentText()
        # 必须一并保存：start_monitor() 会读 config['tdx_host'] 覆盖 price_alert 的节点表，
        # 漏存则界面上填的自定义 TDX 服务器每次重启都丢失（原先就是这个问题）
        self.config['tdx_host'] = self.tdx_host_edit.text().strip()
        # Alpaca：只同步开关状态。凭证由勾选时的弹窗加密写入（api_key_enc/secret_enc），
        # 这里不能重建整个 alpaca 段，否则会把密文凭证字段覆盖丢失
        if not isinstance(self.config.get('alpaca'), dict):
            self.config['alpaca'] = {}
        self.config['alpaca']['enabled'] = self.us_enable_check.isChecked()
        self.config['alerts'] = alerts

        if save_config(self.config):
            self.config_changed.emit(self.config)
            self.accept()
        else:
            QMessageBox.critical(self, '保存失败', '无法写入配置文件，请检查权限。')


# ============================================================
# 主悬浮窗（1/3 宽度）
# ============================================================
class FloatingWindow(QWidget):
    # 美股名称后台补全结果：后台线程不能直接改 UI，统一经此信号回主线程
    us_name_backfilled = Signal(str, str)   # (code, name)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.cards = {}
        self.worker = None
        self.start_time = None
        self._drag_offset = None
        self._kline_tooltip = None
        self._kline_fetcher = None
        self._settings_dialog = None
        self.us_name_backfilled.connect(self._on_us_name_backfilled)

        # 系统托盘
        self._tray_icon = None
        self._build_tray_icon()

        # 初始化主题
        self._theme_mode = self.config.get('theme', 'auto')
        if self._theme_mode == 'auto':
            detected = _ScreenColorDetector(self).detect()
            set_theme(detected)
        else:
            set_theme(self._theme_mode)

        self._init_window()
        self._init_ui()
        self._move_to_bottom_right()

        # auto 模式下每 30 秒检测屏幕颜色（截图 + 200 点采样开销不小，10s 仍偏频繁）
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._check_theme)
        self._theme_timer.start(30000)   # 10s→30s：降低截图与像素采样频率

        QTimer.singleShot(300, self.start_monitor)

    def _init_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumWidth(200)
        self.setMaximumWidth(320)

    def _init_ui(self):
        self.setStyleSheet(f'FloatingWindow {{ background: transparent; }}')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(0)

        self.container = QFrame()
        self.container.setObjectName('container')
        self.container.setStyleSheet(
            f'#container {{'
            f'  background: {get_color("bg")};'
            f'  border-radius: 10px;'
            f'  border: 1px solid {get_color("border")};'
            f'}}'
        )
        outer.addWidget(self.container)

        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(16)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 3)
        self.container.setGraphicsEffect(shadow)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # 标题栏和状态栏已合并到底部栏

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 显式移除 corner widget，彻底清除水平滚动条占用的空间
        self.scroll.setCornerWidget(None)
        self.scroll.setStyleSheet(
            f'QScrollArea {{ background: transparent; border: none; }}'
            f'QScrollBar:vertical {{ background: {get_color("bg_light")}; width: 6px; margin: 0; }}'
            f'QScrollBar::handle:vertical {{ background: {get_color("border")}; border-radius: 3px; min-height: 20px; }}'
            f'QScrollBar::handle:vertical:hover {{ background: {get_color("text_dim")}; }}'
            f'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}'
            f'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}'
            # 水平滚动条：高度设为0，彻底不占空间
            f'QScrollBar:horizontal {{ height: 0; width: 0; background: transparent; }}'
            f'QScrollBar::handle:horizontal {{ background: transparent; }}'
            f'QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ height: 0; width: 0; }}'
        )

        self.list_widget = QWidget()
        self.list_widget.setStyleSheet(f'QWidget {{ background: {get_color("bg")}; }}')
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(2, 2, 2, 2)
        self.list_layout.setSpacing(1)
        self.list_layout.addStretch(0)
        self.scroll.setWidget(self.list_widget)
        container_layout.addWidget(self.scroll, 1)

        # 底部按钮栏
        self._build_bottom_bar(container_layout)
        self._rebuild_cards()



    def _build_bottom_bar(self, parent_layout):
        bar = QFrame()
        bar.setFixedHeight(32)
        bar.setStyleSheet(
            f'background: transparent; border-top: 1px solid {get_color("border")};'
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(4)

        # 左侧：状态点 + 更新时间 + 运行时长
        self.status_dot = QLabel('●')
        self.status_dot.setStyleSheet(f'color: {COLOR_FLAT}; font-size: 9px;')
        h.addWidget(self.status_dot)

        self.update_label = QLabel('--:--')
        self.update_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        h.addWidget(self.update_label)

        self.uptime_label = QLabel('')
        self.uptime_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        h.addWidget(self.uptime_label)

        h.addStretch()

        # 右侧：设置（保存时自动刷新配置）
        self.btn_settings = QPushButton('⚙')
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setFixedSize(24, 20)
        self.btn_settings.setStyleSheet(
            f'QPushButton {{ background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  border: 1px solid {get_color("border")}; border-radius: 4px; font-size: 11px; }}'
            f'QPushButton:hover {{ background: {get_color("border")}; }}'
        )
        self.btn_settings.clicked.connect(self._open_settings)
        h.addWidget(self.btn_settings)

        parent_layout.addWidget(bar)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if self.container.underMouse() or self.childAt(event.pos()) is None:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            # 钳制在屏幕内：拖出边界后既看不见也点不到（双击关闭也够不着），
            # 而托盘「显示」只是 show()，窗口等于找不回
            self.move(self._clamp_into_screen(event.globalPosition().toPoint() - self._drag_offset))
            event.accept()

    def _clamp_into_screen(self, pos: QPoint) -> QPoint:
        """把窗口左上角限制在「光标所在屏幕」的可见区域内"""
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        w, h = self.width() or 140, self.height() or 400
        return QPoint(max(geo.left(), min(pos.x(), geo.right() - w)),
                      max(geo.top(), min(pos.y(), geo.bottom() - h)))

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """双击任意位置关闭程序"""
        if event.button() == Qt.LeftButton:
            self.close()

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
        quit_action.triggered.connect(self.close)
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
        """点击托盘图标显示窗口"""
        if reason == QSystemTrayIcon.Trigger:
            self.show()
            # 窗口曾被拖到屏幕外的话，只 show() 依然看不见：拉回可见区域
            self.move(self._clamp_into_screen(self.pos()))
            self.raise_()
            self.activateWindow()

    def _move_to_bottom_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        w, h = self.width() or 140, self.height() or 400
        x = screen.right() - w - 20
        y = screen.bottom() - h - 20
        self.move(max(0, x), max(0, y))

    def _rebuild_cards(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.cards.clear()

        alerts = self.config.get('alerts', [])
        seen = {}
        for item in alerts:
            code = item.get('code', '')
            if code in seen:
                continue
            seen[code] = item
            name = item.get('name', code)
            card = QuoteCard(code, name)
            card.set_data({
                'price': None, 'change_pct': None,
                'target': item.get('target'),
                'direction': item.get('dir', 'below'),
                'target_pct': item.get('target_pct'),
                'pct_dir': item.get('pct_dir', 'below'),
                'position': item.get('position', ''),
                'note': item.get('note', ''),
                'last_update': '--',
            })
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.cards[code] = card

        n = len(self.cards)
        # 精确计算高度：每张卡片24px + 间距1px + 边距4px
        # 最多显示10条，超过则需要滚动
        MAX_VISIBLE = 10
        CARD_H = 24
        CARD_SPACING = 1
        LIST_MARGIN_H = 4   # 列表上下边距
        BOTTOM_BAR_H = 32   # 底部按钮栏
        OUTER_MARGIN_H = 12  # 外框上下边距
        CONTAINER_BORDER_H = 2  # 容器1px边框上下
        BUFFER_H = 4  # 补偿DPI和渲染误差

        visible_n = min(n, MAX_VISIBLE)
        if visible_n > 0:
            list_content_h = visible_n * CARD_H + max(0, visible_n - 1) * CARD_SPACING + LIST_MARGIN_H
        else:
            list_content_h = 0
        total_h = OUTER_MARGIN_H + CONTAINER_BORDER_H + list_content_h + BOTTOM_BAR_H + BUFFER_H

        self.resize(280, total_h)
        # 重置滚动区域最小高度
        self.scroll.setMinimumHeight(0)

    def _check_theme(self):
        """auto 模式下定期检测屏幕颜色，切换主题"""
        if self._theme_mode != 'auto':
            return
        detected = _ScreenColorDetector(self).detect()
        if detected != get_current_theme():
            set_theme(detected)
            self._apply_theme()

    def _apply_theme(self):
        """主题切换后刷新所有界面元素"""
        # 容器
        self.container.setStyleSheet(
            f'#container {{'
            f'  background: {get_color("bg")};'
            f'  border-radius: 10px;'
            f'  border: 1px solid {get_color("border")};'
            f'}}'
        )
        # 滚动条
        self.scroll.setStyleSheet(
            f'QScrollArea {{ background: transparent; border: none; }}'
            f'QScrollBar:vertical {{ background: {get_color("bg_light")}; width: 6px; margin: 0; }}'
            f'QScrollBar::handle:vertical {{ background: {get_color("border")}; border-radius: 3px; min-height: 20px; }}'
            f'QScrollBar::handle:vertical:hover {{ background: {get_color("text_dim")}; }}'
            f'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}'
            f'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}'
            f'QScrollBar:horizontal {{ height: 0; width: 0; background: transparent; }}'
            f'QScrollBar::handle:horizontal {{ background: transparent; }}'
            f'QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ height: 0; width: 0; }}'
        )
        # 列表背景
        self.list_widget.setStyleSheet(f'QWidget {{ background: {get_color("bg")}; }}')
        # 底栏标签
        self.update_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        self.uptime_label.setStyleSheet(f'color: {get_color("text_dim")}; font-size: 9px;')
        self.btn_settings.setStyleSheet(
            f'QPushButton {{ background: {get_color("bg_light")}; color: {get_color("text")};'
            f'  border: 1px solid {get_color("border")}; border-radius: 4px; font-size: 11px; }}'
            f'QPushButton:hover {{ background: {get_color("border")}; }}'
        )
        # 刷新所有卡片
        for card in self.cards.values():
            card._on_theme_changed()

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        dlg.config_changed.connect(self._on_config_changed)
        self._settings_dialog = dlg
        dlg.exec()
        # 对话框以 self 为 parent，exec 返回后仍作为子对象存活（含整张表）：
        # 每打开一次就泄漏一份，长期挂机 + 频繁改设置会持续吃内存
        dlg.deleteLater()

    def _on_config_changed(self, new_config):
        self.config = new_config
        # 检查主题是否变化
        new_theme = new_config.get('theme', 'auto')
        if new_theme != self._theme_mode:
            self._theme_mode = new_theme
            if new_theme == 'auto':
                detected = _ScreenColorDetector(self).detect()
                set_theme(detected)
            else:
                set_theme(new_theme)
            self._apply_theme()
        old = self.worker
        was_running = old is not None and old.isRunning()
        if was_running:
            old.stop()
            if not old.wait(2000):
                # 旧线程还卡在一次取数里。此时直接 start_monitor() 会被它的
                # isRunning() 守卫挡掉，新线程永远建不起来 —— 行情从此永久停更。
                print('[Monitor] 旧取数线程 2 秒内未退出，等它真正结束后自动重启')
                old.finished.connect(self._restart_monitor)
        self._rebuild_cards()
        if was_running and old.isFinished():
            self.start_monitor()

    def _apply_alpaca_config(self):
        """把 Alpaca 配置注入数据层，并按监控列表启停 WebSocket 线程

        启动与「设置保存」两条路径都经过这里，保证凭证/订阅列表变更即时生效。
        """
        alc = self.config.get('alpaca') or {}
        # 凭证从密文字段解密后只进内存（alpaca_history 模块级），不打印到任何日志
        key, secret = secret_store.load_alpaca_creds(alc)
        alpaca_history.configure(key, secret)
        us_codes = [
            normalize_us_symbol(item.get('code', ''))
            for item in self.config.get('alerts', [])
        ]
        us_codes = [c for c in us_codes if c]
        if alc.get('enabled') and us_codes:
            alpaca_stream.ensure_started(us_codes)
        else:
            alpaca_stream.stop()

    def _backfill_us_names(self):
        """后台给名称为空的美股代码自动补名称

        场景：设置里直接输 AAPL 时，若凭证还没配置好（或 assets 端点当时不可用），
        名称解析会留空并保存；凭证生效后由本方法兜底补全，免去用户手动重输代码。
        """
        if not alpaca_history.has_credentials():
            print('[Alpaca] 名称补全跳过：未配置凭证')
            return
        todo = [
            item.get('code')
            for item in self.config.get('alerts', [])
            if is_us_symbol(item.get('code', '')) and not item.get('name')
        ]
        todo = list(dict.fromkeys(todo))          # 去重保序
        print(f'[Alpaca] 名称补全检查: 待补={todo}')
        if not todo:
            return

        def work():
            for code in todo:
                try:
                    name = alpaca_history.get_symbol_name(code)
                except Exception as e:
                    print(f'[Alpaca] 名称补全异常 {code}: {e}')
                    name = ''
                print(f'[Alpaca] 名称补全 {code} -> {name!r}')
                if name:
                    self.us_name_backfilled.emit(code, name)

        threading.Thread(target=work, daemon=True, name='us-name-backfill').start()

    def _on_us_name_backfilled(self, code, name):
        """补名结果（主线程执行）：写配置 + 落盘 + 刷新卡片名称显示"""
        changed = False
        for item in self.config.get('alerts', []):
            if item.get('code') == code and not item.get('name'):
                item['name'] = name
                changed = True
        if changed:
            save_config(self.config)
        card = self.cards.get(code)
        if card:
            card.name = name
            card._update_name_elided()

    def _restart_monitor(self):
        """旧取数线程真正退出后补一次重启（见 _on_config_changed 的超时分支）"""
        if self.worker is not None and self.worker.isRunning():
            return
        self.start_monitor()

    def start_monitor(self):
        if self.worker and self.worker.isRunning():
            return
        self.config = load_config()
        # 应用TDX自定义节点配置
        tdx_host = self.config.get('tdx_host', '')
        if tdx_host:
            import price_alert
            price_alert.TDX_NODES = [
                {'id': 'custom', 'name': f'自定义({tdx_host})', 'host': tdx_host, 'priority': 1}
            ]
            price_alert.DEFAULT_TDX_HOST = tdx_host
        # 美股（Alpaca）：注入凭证 + 启动实时 WebSocket（历史K线由 REST 按需拉取）
        self._apply_alpaca_config()
        self._rebuild_cards()
        # 凭证就绪后，给名称为空的美股代码后台补名称（首次会全量拉 assets，约1-2秒）
        self._backfill_us_names()
        self.worker = QuoteWorker(self.config)
        self.worker.quote_updated.connect(self.on_quote)
        self.worker.alert_triggered.connect(self.on_alert)
        self.worker.status_changed.connect(self.on_status)
        self.worker.batch_done.connect(self.on_batch_done)
        self.worker.start()
        self.start_time = datetime.datetime.now()
        # 复用同一个定时器：原先每次重启都 new 一个 QTimer(self)，旧的一直在 1 秒一跳，
        # 每保存一次设置就泄漏一个活动定时器（长跑进程内存持续增长）
        if not hasattr(self, '_uptime_timer'):
            self._uptime_timer = QTimer(self)
            self._uptime_timer.timeout.connect(self._update_uptime)
        self._uptime_timer.start(1000)


    def stop_monitor(self):
        if self.worker:
            self.worker.stop()
            if not self.worker.wait(2000):
                # 线程还在跑时不能丢引用：QThread 对象被 GC 会在运行中析构，Qt 直接 abort
                print('[Monitor] 取数线程 2 秒内未退出，保留引用直到其自行结束')
            else:
                self.worker = None
        alpaca_stream.stop()   # 停实时流线程（daemon，随进程退出也会自己结束）
        if hasattr(self, '_uptime_timer'):
            self._uptime_timer.stop()
        self.status_dot.setStyleSheet(f'color: {COLOR_FLAT}; font-size: 9px;')

    def show_kline_for_card(self, code, name, mouse_pos, period='1min'):
        """Show kline tooltip instantly from cache (zero latency)
        period: '1min' | 'daily' | 'weekly'
        """
        cache_key = code if period == '1min' else f'{code}_{period}'
        klines = []
        if self.worker and hasattr(self.worker, '_kline_get'):
            cached = self.worker._kline_get(cache_key)
            if cached:
                klines = cached[1]
        # 有缓存则显示；无缓存（日K/周K尚未加载）也显示空态，保持位置稳定
        self._show_kline_tooltip(code, name, klines, mouse_pos, period)

    def _show_kline_tooltip(self, code, name, klines, mouse_pos, period='1min'):
        """显示K线悬浮图, 智能定位
        切换周期时若tooltip已可见，仅更新数据不重定位，避免图跳动
        """
        size_key = self.config.get('kline_size', 'small')
        if self._kline_tooltip is None:
            self._kline_tooltip = KlineTooltip(size_key, main_window=self)
        elif self._kline_tooltip.size_key != size_key if hasattr(self._kline_tooltip, 'size_key') else True:
            # 尺寸变更时重建
            self._kline_tooltip.close()
            self._kline_tooltip = KlineTooltip(size_key, main_window=self)

        already_visible = self._kline_tooltip.isVisible()
        self._kline_tooltip.set_data(code, name, klines)

        # 仅在首次显示时定位；切周期时保持位置不变
        if not already_visible:
            screen = QApplication.primaryScreen().availableGeometry()
            tip_w = self._kline_tooltip.width()
            tip_h = self._kline_tooltip.height()

            # 默认: 鼠标右下方
            x = mouse_pos.x() + 15
            y = mouse_pos.y() + 15
            # 右侧不够 → 左侧
            if x + tip_w > screen.right():
                x = mouse_pos.x() - tip_w - 15
            # 下方不够 → 上方
            if y + tip_h > screen.bottom():
                y = mouse_pos.y() - tip_h - 15
            # 确保不超出屏幕
            x = max(screen.left(), min(x, screen.right() - tip_w))
            y = max(screen.top(), min(y, screen.bottom() - tip_h))

            # 避让悬浮窗本体：K线图现在可接收鼠标（双击关闭），若盖住悬浮窗
            # 会挡住卡片悬停导致图反复消失，因此优先放到悬浮窗外侧
            main_geo = self.frameGeometry()
            overlap = not (x + tip_w < main_geo.left() or x > main_geo.right()
                           or y + tip_h < main_geo.top() or y > main_geo.bottom())
            if overlap:
                # 优先: 贴到悬浮窗正上方
                y_top = main_geo.top() - tip_h - 4
                if y_top >= screen.top():
                    y = y_top
                else:
                    # 其次: 贴到悬浮窗正左侧
                    x_left = main_geo.left() - tip_w - 4
                    if x_left >= screen.left():
                        x = x_left
                    else:
                        # 放不下: 贴悬浮窗顶边，允许少量重叠
                        y = max(screen.top(), main_geo.top() - tip_h)
                # 避让后再次确保不出屏幕
                x = max(screen.left(), min(x, screen.right() - tip_w))
                y = max(screen.top(), min(y, screen.bottom() - tip_h))

            self._kline_tooltip.move(x, y)

        self._kline_tooltip.show()
        self._kline_tooltip.raise_()

    def hide_kline_tooltip(self):
        """隐藏K线悬浮图"""
        if self._kline_tooltip:
            self._kline_tooltip.hide()

    def on_quote(self, data):
        code = data.get('code')
        card = self.cards.get(code)
        if card:
            card.set_data(data)

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
        # 仅在窗口不可见时才唤起，避免每次预警都强抢焦点打断用户操作
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()

    def on_status(self, status):
        if status == 'running':
            self.status_dot.setStyleSheet(f'color: {COLOR_DOWN}; font-size: 9px;')
        elif status == 'stopped':
            self.status_dot.setStyleSheet(f'color: {COLOR_FLAT}; font-size: 9px;')
        elif status == 'error':
            self.status_dot.setStyleSheet(f'color: {COLOR_ALERT}; font-size: 9px;')

    def on_batch_done(self, now_str):
        self.update_label.setText(now_str)

    def _update_uptime(self):
        if not self.start_time:
            return
        delta = datetime.datetime.now() - self.start_time
        total = int(delta.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            self.uptime_label.setText(f'{h}:{m:02d}:{s:02d}')
        else:
            self.uptime_label.setText(f'{m}:{s:02d}')

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
        try:
            alpaca_stream.stop()
        except Exception:
            pass
        try:
            close_tdx_client()
        except Exception:
            pass
        event.accept()
        # 清理完毕后真正退出进程：app 设了 setQuitOnLastWindowClosed(False)，
        # 仅 accept 不会终止进程；双击/托盘「退出」都走这里，需显式 quit
        QApplication.quit()



# ============================================================
# 入口
# ============================================================
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName('Stock Monitor')
    font = QFont('Microsoft YaHei UI', 9)
    app.setFont(font)

    window = FloatingWindow()
    window.show()

    print('=' * 50)
    print(f'  Stock Monitor Floating Window v{VERSION}')
    print(f'  Started: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'  Config: {CONFIG_FILE}')
    print('=' * 50)

    sys.exit(app.exec())


if __name__ == '__main__':
    main()

