#!/usr/bin/env python3
"""
floating_window.py — Stock Monitor 桌面悬浮窗 v5.0
PySide6 原生 Win11 风格悬浮窗：无边框、置顶、半透明、可拖拽
复用 price_alert.py 的数据获取逻辑（支持 A股/港股/美股/韩股）
紧凑版：1/3 宽度，支持目标价 + 单日涨跌% 双预警
"""

import os
import sys
import json
import time
import datetime
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scripts'))
from price_alert import get_quote, detect_market, close_tdx_client, get_quotes_batch, get_kline_1min

# 调试日志（空实现，避免修改所有调用处）
def _log(msg):
    pass

from PySide6.QtCore import Qt, QThread, Signal, QPoint, QRect, QTimer, QSize
from PySide6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPainter, QBrush, QPen,
    QMouseEvent, QPaintEvent, QAction, QIntValidator, QDoubleValidator, QCursor
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QFrame, QGraphicsDropShadowEffect, QScrollArea,
    QMessageBox, QSizePolicy,
    QDialog, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFormLayout, QGroupBox, QSpinBox
)

# ============================================================
# 配置
# ============================================================
VERSION = "5.0"
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'scripts', 'watchlist_config.json')

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


# ============================================================
# 配置读写
# ============================================================
def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as e:
        print(f'Error loading config: {e}')
        return {'interval': 5, 'source': 'auto', 'alerts': [], 'theme': 'auto'}


def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'Error saving config: {e}')
        return False


def is_a_share_trading_time(now=None):
    """判断当前是否在A股交易时间段
    交易时段：09:10-11:30, 13:00-15:00（周末不交易）
    """
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:  # 周六、周日
        return False
    minutes = now.hour * 60 + now.minute
    # 9:10=550, 11:30=690, 13:00=780, 15:00=900
    if 550 <= minutes <= 690:
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
        self._kline_fetching = False
        self._kline_period_fetching = False
        self._first_loop = True     # 首次循环标记（启动时无论是否交易时间都请求一次，避免空白）

    def update_config(self, config):
        with self._lock:
            self.config = config

    def stop(self):
        self.running = False

    def run(self):
        self.running = True
        self.status_changed.emit('running')
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
            valid_codes = [item.get('code', '') for item in alerts if item.get('code')]

            # A股交易时间外不刷新行情（首次启动除外，避免空白）
            # 非A股代码（美股/港股/韩股）不受此限制
            a_codes_in_list = [c for c in valid_codes if detect_market(c) == 'a_share']
            non_a_codes = [c for c in valid_codes if detect_market(c) != 'a_share']
            if non_a_codes:
                should_fetch = True
            elif a_codes_in_list:
                should_fetch = is_first_loop or is_a_share_trading_time()
            else:
                should_fetch = False

            if should_fetch:
                results = get_quotes_batch(valid_codes, source)
            else:
                results = {}
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

                # Yahoo returns klines in chart data, cache them directly
                klines = q.get('klines', [])
                if klines:
                    self.kline_cache[code] = (time.time(), klines)

                payload = {
                    'name': name,
                    'code': code,
                    'market': detect_market(code),
                    'price': price,
                    'change_pct': q.get('change_pct'),
                    'yest_close': q.get('yest_close'),
                    'target': target,
                    'direction': direction,
                    'target_pct': target_pct,
                    'pct_dir': pct_dir,
                    'position': position,
                    'note': note,
                    'currency': q.get('currency', ''),
                    'session': q.get('session', ''),
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
                a_codes = [item.get('code', '') for item in alerts
                           if item.get('code') and detect_market(item['code']) == 'a_share']
                codes_to_fetch = []
                now_ts = time.time()
                for c in a_codes:
                    last_ts = self.kline_cache.get(c, (0, []))[0]
                    if now_ts - last_ts > 30:
                        codes_to_fetch.append(c)
                if codes_to_fetch:
                    t = threading.Thread(target=self._fetch_a_share_klines, args=(codes_to_fetch,), daemon=True)
                    t.start()
                else:
                    self._kline_fetching = False

            # 日K/周K 异步预加载（启动时请求一次，之后每小时刷新一次）
            # 非交易时间跳过刷新（首次启动除外，避免空白）
            if not self._kline_period_fetching and (is_first_loop or is_a_share_trading_time()):
                period_pairs = []
                now_ts = time.time()
                # 所有市场都支持日K/周K（A股走东方财富，美股/韩股/港股走Yahoo）
                all_codes = [item.get('code', '') for item in alerts if item.get('code')]
                for c in all_codes:
                    for period in ('daily', 'weekly'):
                        cache_key = f'{c}_{period}'
                        last_ts = self.kline_cache.get(cache_key, (0, []))[0]
                        if now_ts - last_ts > 3600:  # 1小时
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

        self.status_changed.emit('stopped')
        try:
            close_tdx_client()
        except Exception:
            pass

    def _fetch_a_share_klines(self, codes):
        """后台线程: 获取A股1分钟K线并缓存"""
        try:
            for code in codes:
                if not self.running:
                    break
                try:
                    klines = get_kline_1min(code, 60)
                    if klines:
                        self.kline_cache[code] = (time.time(), klines)
                except Exception as e:
                    print(f'[Kline Prefetch] {code} failed: {e}')
        finally:
            self._kline_fetching = False

    def _fetch_kline_period(self, code_period_pairs):
        """后台线程: 获取日K/周K并缓存（启动时+每小时刷新）"""
        try:
            from price_alert import get_kline
            for code, period in code_period_pairs:
                if not self.running:
                    break
                try:
                    klines = get_kline(code, period, 60)
                    if klines:
                        cache_key = f'{code}_{period}'
                        self.kline_cache[cache_key] = (time.time(), klines)
                except Exception as e:
                    print(f'[Kline {period}] {code} failed: {e}')
        finally:
            self._kline_period_fetching = False

    def _check_alert(self, name, code, price, target, direction):
        if direction in ('below', 'both') and price <= target:
            return True, f'{name} 跌破 {target} | 当前: {price:.2f}'
        if direction in ('above', 'both') and price >= target:
            return True, f'{name} 涨破 {target} | 当前: {price:.2f}'
        return False, ''

    def _check_pct_alert(self, name, code, change_pct, target_pct, pct_dir):
        """涨跌幅预警：默认用绝对值比较（写3则涨跌3%都触发）
        pct_dir: below=仅跌, above=仅涨, both=绝对值(默认)
        """
        abs_change = abs(change_pct)
        abs_target = abs(target_pct)
        if pct_dir == 'below':
            if change_pct <= target_pct:
                return True, f'{name} 跌幅达 {change_pct:.2f}% ≤ {target_pct}%'
        elif pct_dir == 'above':
            if change_pct >= target_pct:
                return True, f'{name} 涨幅达 {change_pct:.2f}% ≥ {target_pct}%'
        else:  # both: 绝对值，涨跌都触发
            if abs_change >= abs_target:
                direction = '跌' if change_pct < 0 else '涨'
                return True, f'{name} {direction}幅达 {change_pct:+.2f}% 超过 {abs_target}%'
        return False, ''



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
    """K线悬浮图窗口（无边框、置顶、鼠标穿透）"""

    def __init__(self, size_key='small'):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        w, h = KLINE_SIZES.get(size_key, KLINE_SIZES['small'])
        self.setFixedSize(w, h)

        self.code = ''
        self.name = ''
        self.klines = []

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

        # 涨跌幅
        if first['close'] > 0:
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
        bar_w = max(2, (w - n) // n)
        gap = 1
        step = bar_w + gap

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
            x = x0 + i * step
            open_y = price_to_y(k['open'])
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
        self.setAttribute(Qt.WA_Hover, True)

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
        self._hover_timer.start(300)
        self._show_period_buttons()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开卡片: 取消请求并隐藏K线图和按钮"""
        self._hover_timer.stop()
        for btn in self._period_buttons.values():
            btn.setVisible(False)
        win = self.window()
        if hasattr(win, 'hide_kline_tooltip'):
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
        self.setStyleSheet(
            f'#quoteCard {{'
            f'  background: {COLOR_TRIGGERED};'
            f'  border: 2px solid #ffffff;'
            f'  border-radius: 6px;'
            f'}}'
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
        market = data.get('market', '')
        currency = data.get('currency', '')
        last_update = data.get('last_update', '')
        session = data.get('session', '')
        name = data.get('name', self.name)

        # 更新名称（如有变化）
        if name != self.name:
            self.name = name
            self._update_name_elided()

        # 货币符号
        currency_symbol = ''
        if currency == 'USD':
            currency_symbol = '$'
        elif currency == 'KRW':
            currency_symbol = '₩'
        elif currency == 'HKD':
            currency_symbol = 'HK$'
        elif market == 'a_share':
            currency_symbol = '¥'

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
            if direction in ('below', 'both') and price <= target:
                triggered = True
                trigger_reason = f'{DIR_LABELS.get(direction, direction)}{target}'
            if direction in ('above', 'both') and price >= target:
                triggered = True
                trigger_reason = f'{DIR_LABELS.get(direction, direction)}{target}'

        if not triggered and target_pct is not None and change_pct is not None:
            pct_dir = data.get('pct_dir', 'both')
            abs_change = abs(change_pct)
            abs_target = abs(target_pct)
            if pct_dir == 'below' and change_pct <= target_pct:
                triggered = True
                trigger_reason = f'跌≤{target_pct}%'
            elif pct_dir == 'above' and change_pct >= target_pct:
                triggered = True
                trigger_reason = f'涨≥{target_pct}%'
            elif abs_change >= abs_target:  # both: 绝对值
                triggered = True
                direction_cn = '跌' if change_pct < 0 else '涨'
                trigger_reason = f'{direction_cn}≥{abs_target}%'

        # 状态显示：不显示预警文字，仅通过绿色底色表示触发
        self.status_label.setText('')

        # 只在状态变化时切换样式
        if triggered and not self._triggered:
            self._apply_triggered_style()
        elif not triggered and self._triggered:
            self._apply_default_style()

        # 市场标签
        market_text = {
            'a_share': 'A', 'hk': '港', 'us': '美', 'kr': '韩', 'yahoo': 'Y'
        }.get(market, '')
        session_colors = {
            '盘前': '#f39c12', '盘后': '#9b59b6', '已休市': COLOR_FLAT
        }
        if session and session != '常规':
            self.market_label.setText(market_text)
            color = session_colors.get(session, COLOR_FLAT)
            self.market_label.setStyleSheet(
                f'color: {color}; font-size: 9px;'
                f'border: 1px solid {color}; border-radius: 2px;'
            )
        else:
            self.market_label.setText(market_text)
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
        _log(f'[Thread] run() 开始, input="{self.input_text}"')
        candidates = []
        try:
            from price_alert import get_quote_tencent
            text = self.input_text.strip()
            lower_text = text.lower()

            # 已带前缀
            if lower_text.startswith(('sh', 'sz', 'hk', 'us', 'bj')):
                if lower_text.startswith('us') and text[2:].islower():
                    text = 'us' + text[2:].upper()
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

            # 纯字母：美股
            elif text.isalpha():
                full_code = 'us' + text.upper()
                try:
                    q = get_quote_tencent(full_code)
                    name = q.get('name', '')
                    price = q.get('price')
                    if name and name != full_code and price and price > 0:
                        candidates = [(full_code, name)]
                    else:
                        candidates = [(full_code, '')]
                except Exception:
                    candidates = [(full_code, '')]

            _log(f'[Thread] run() 完成, candidates={candidates}')
        except Exception as e:
            import traceback
            _log(f'[Thread] run() 异常: {e}\n{traceback.format_exc()}')

        self.finished_resolve.emit(candidates)


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
        _log('[SettingsDialog] 初始化完成, cellChanged已连接')

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
        self.source_combo.addItems(['auto', 'tencent', 'yahoo', 'tdx'])
        self.source_combo.setCurrentText(self.config.get('source', 'auto'))
        top_form.addRow('数据源:', self.source_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(['auto', 'dark', 'light'])
        self.theme_combo.setCurrentText(self.config.get('theme', 'auto'))
        top_form.addRow('主题:', self.theme_combo)
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
            del_btn.clicked.connect(lambda _=False, r=row: self._delete_row(r))
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
        del_btn.clicked.connect(lambda _=False, r=row: self._delete_row(r))
        self.table.setCellWidget(row, 8, del_btn)
        self.table.setCurrentCell(row, 0)

    def _delete_row(self, row):
        self.table.removeRow(row)

    def _on_code_changed(self, row, col):
        """代码单元格变化时，启动异步线程搜索名称（避免阻塞UI）"""
        _log(f'[cellChanged] row={row}, col={col}, block={self._block_signals}, loading={getattr(self, "_loading", False)}')
        if self._block_signals or getattr(self, '_loading', False):
            return
        if col != 0:  # 只处理代码列
            return

        item = self.table.item(row, col)
        if not item:
            return
        code = item.text().strip()
        _log(f'[cellChanged] code="{code}"')
        if not code:
            return

        # 已带前缀的不再重复查询
        if code.lower().startswith(('sh', 'sz', 'hk', 'us', 'bj')):
            _log(f'[cellChanged] 已带前缀，跳过')
            return

        name_item = self.table.item(row, 1)

        # 如果上一个线程还在跑，等它结束
        if self._resolve_thread and self._resolve_thread.isRunning():
            self._resolve_thread.quit()
            self._resolve_thread.wait(2000)

        # 启动异步查询
        self._resolve_thread = _StockResolveThread(code)
        self._resolve_thread._row = row
        self._resolve_thread._item = item
        self._resolve_thread._name_item = name_item
        self._resolve_thread.finished_resolve.connect(self._on_resolve_done)
        self._resolve_thread.start()
        _log(f'[cellChanged] 线程已启动')

    def _on_resolve_done(self, candidates):
        """异步查询完成回调（在主线程执行）"""
        _log(f'[resolve_done] candidates={candidates}')
        if not candidates:
            return

        item = getattr(self._resolve_thread, '_item', None)
        name_item = getattr(self._resolve_thread, '_name_item', None)
        _log(f'[resolve_done] item={item is not None}, name_item={name_item is not None}')
        if item is None:
            return

        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
            _log(f'[resolve_done] 单候选, chosen={chosen}')
        else:
            # 多个候选，弹窗选择
            labels = [f'{c[0]}  {c[1]}' for c in candidates]
            _log(f'[resolve_done] 多候选={len(candidates)}, 即将弹窗: {labels}')
            from PySide6.QtWidgets import QInputDialog
            sel, ok = QInputDialog.getItem(
                self, '选择股票',
                f'匹配到以下 {len(candidates)} 只股票，请选择：',
                labels, 0, False
            )
            _log(f'[resolve_done] 弹窗结果: ok={ok}, sel={sel}')
            if ok and sel:
                chosen = candidates[labels.index(sel)]

        if chosen:
            self._block_signals = True
            item.setText(chosen[0])
            if name_item:
                name_item.setText(chosen[1] or '')
            self._block_signals = False
            _log(f'[resolve_done] 已填充: code={chosen[0]}, name={chosen[1]}')

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

        # auto 模式下每 10 秒检测屏幕颜色
        self._theme_timer = QTimer(self)
        self._theme_timer.timeout.connect(self._check_theme)
        self._theme_timer.start(3000)

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
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_offset = None

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """双击任意位置关闭程序"""
        if event.button() == Qt.LeftButton:
            QApplication.quit()

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
                'market': detect_market(code),
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
        was_running = self.worker and self.worker.isRunning()
        if was_running:
            self.worker.stop()
            self.worker.wait(2000)
        self._rebuild_cards()
        if was_running:
            self.start_monitor()

    def start_monitor(self):
        if self.worker and self.worker.isRunning():
            return
        self.config = load_config()
        self._rebuild_cards()
        self.worker = QuoteWorker(self.config)
        self.worker.quote_updated.connect(self.on_quote)
        self.worker.alert_triggered.connect(self.on_alert)
        self.worker.status_changed.connect(self.on_status)
        self.worker.batch_done.connect(self.on_batch_done)
        self.worker.start()
        self.start_time = datetime.datetime.now()
        self._uptime_timer = QTimer(self)
        self._uptime_timer.timeout.connect(self._update_uptime)
        self._uptime_timer.start(1000)


    def stop_monitor(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        if hasattr(self, '_uptime_timer'):
            self._uptime_timer.stop()
        self.status_dot.setStyleSheet(f'color: {COLOR_FLAT}; font-size: 9px;')

    def show_kline_for_card(self, code, name, mouse_pos, period='1min'):
        """Show kline tooltip instantly from cache (zero latency)
        period: '1min' | 'daily' | 'weekly'
        """
        cache_key = code if period == '1min' else f'{code}_{period}'
        klines = []
        if self.worker and hasattr(self.worker, 'kline_cache'):
            cached = self.worker.kline_cache.get(cache_key)
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
            self._kline_tooltip = KlineTooltip(size_key)
        elif self._kline_tooltip.size_key != size_key if hasattr(self._kline_tooltip, 'size_key') else True:
            # 尺寸变更时重建
            self._kline_tooltip.close()
            self._kline_tooltip = KlineTooltip(size_key)

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
        pass  # 预警仅触发卡片变色，不再弹通知

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
            close_tdx_client()
        except Exception:
            pass
        event.accept()



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

