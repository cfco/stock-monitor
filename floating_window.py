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

from PySide6.QtCore import Qt, QThread, Signal, QPoint, QRect, QTimer, QSize
from PySide6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPainter, QBrush, QPen,
    QMouseEvent, QPaintEvent, QAction, QIntValidator, QDoubleValidator, QCursor
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QPushButton, QFrame, QGraphicsDropShadowEffect, QScrollArea,
    QSystemTrayIcon, QMenu, QMessageBox, QSizePolicy,
    QDialog, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QFormLayout, QGroupBox, QSpinBox
)

# ============================================================
# 配置
# ============================================================
VERSION = "5.0"
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'scripts', 'watchlist_config.json')

# 颜色
COLOR_UP = '#e74c3c'
COLOR_DOWN = '#2ecc71'
COLOR_FLAT = '#95a5a6'
COLOR_BG = '#1a1a2e'
COLOR_BG_LIGHT = '#252540'
COLOR_TEXT = '#ffffff'
COLOR_TEXT_DIM = '#aaaaaa'
COLOR_BORDER = '#3a3a55'
COLOR_ALERT = '#ff5555'
COLOR_TRIGGERED = '#2ecc71'
COLOR_DEFAULT_CARD = '#1a1a2e'

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
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'Error loading config: {e}')
        return {'interval': 5, 'source': 'auto', 'alerts': []}


def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'Error saving config: {e}')
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
        self.kline_cache = {}       # {code: (timestamp, klines)}
        self._kline_fetching = False

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

            # 批量获取行情（按数据源分组，减少请求次数）
            fetch_start = time.time()
            valid_codes = [item.get('code', '') for item in alerts if item.get('code')]
            results = get_quotes_batch(valid_codes, source)

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
            if not self._kline_fetching:
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
class KlineTooltip(QWidget):
    """K线悬浮图窗口（无边框、置顶、鼠标穿透）"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFixedSize(320, 180)

        self.code = ''
        self.name = ''
        self.klines = []

    def set_data(self, code, name, klines):
        self.code = code
        self.name = name
        self.klines = klines
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # 背景
        bg_color = QColor(COLOR_BG)
        bg_color.setAlpha(245)
        p.setBrush(QBrush(bg_color))
        p.setPen(QPen(QColor(COLOR_BORDER), 1))
        p.drawRoundedRect(0, 0, self.width(), self.height(), 8, 8)

        if not self.klines:
            p.setPen(QColor(COLOR_TEXT_DIM))
            p.setFont(QFont('Microsoft YaHei UI', 9))
            p.drawText(self.rect(), Qt.AlignCenter, '暂无K线数据')
            p.end()
            return

        # 标题: 名称 + 最新价
        last = self.klines[-1]
        first = self.klines[0]
        p.setPen(QColor(COLOR_TEXT))
        p.setFont(QFont('Microsoft YaHei UI', 9, QFont.Bold))
        title = f'{self.name}  {last["close"]:.2f}'
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
        p.setPen(QColor(COLOR_TEXT_DIM))
        p.setFont(QFont('Microsoft YaHei UI', 7))
        p.drawText(x0 + 2, y0 + 8, f'{max_price:.2f}')
        p.drawText(x0 + 2, y0 + h, f'{min_price:.2f}')

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
        self.name_label.setToolTip(name)
        self.name_label.setStyleSheet(
            f'color: {COLOR_TEXT}; font-size: 10px; font-weight: 600;'
        )
        self._update_name_elided()
        layout.addWidget(self.name_label)

        # 市场标签
        self.market_label = QLabel('')
        self.market_label.setFixedWidth(16)
        self.market_label.setAlignment(Qt.AlignCenter)
        self.market_label.setStyleSheet(
            f'color: {COLOR_TEXT_DIM}; font-size: 8px;'
            f'border: 1px solid {COLOR_BORDER}; border-radius: 2px;'
        )
        layout.addWidget(self.market_label)

        # 价格（固定宽度 + 右对齐）
        self.price_label = QLabel('--')
        self.price_label.setFixedWidth(70)
        self.price_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.price_label.setStyleSheet(
            f'color: {COLOR_TEXT}; font-size: 12px; font-weight: 700;'
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
        self.status_label.setStyleSheet(f'color: {COLOR_TEXT_DIM}; font-size: 9px;')
        layout.addWidget(self.status_label, 1)

        # 更新时间
        self.time_label = QLabel('')
        self.time_label.setFixedWidth(48)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_label.setStyleSheet(f'color: {COLOR_TEXT_DIM}; font-size: 8px;')
        layout.addWidget(self.time_label)

        self._apply_default_style()

        # K线悬浮图: hover防抖定时器（用QTimer实例，便于stop）
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._request_kline)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, event):
        """鼠标进入卡片: 300ms防抖后请求K线"""
        self._hover_timer.start(300)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开卡片: 取消请求并隐藏K线图"""
        self._hover_timer.stop()
        win = self.window()
        if hasattr(win, 'hide_kline_tooltip'):
            win.hide_kline_tooltip()
        super().leaveEvent(event)

    def _request_kline(self):
        """请求该股票的1分钟K线"""
        win = self.window()
        if hasattr(win, 'show_kline_for_card'):
            win.show_kline_for_card(self.code, self.name, QCursor.pos())

    def _update_name_elided(self):
        """截断长名称显示，鼠标悬停显示完整"""
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.name_label.font())
        elided = fm.elidedText(self.name, Qt.ElideRight, self.name_label.width() - 4)
        self.name_label.setText(elided)
        self.name_label.setToolTip(self.name)

    def _apply_default_style(self):
        self._triggered = False
        self.setStyleSheet(
            f'#quoteCard {{'
            f'  background: {COLOR_DEFAULT_CARD};'
            f'  border: 1px solid {COLOR_BORDER};'
            f'  border-radius: 6px;'
            f'}}'
        )

    def _apply_triggered_style(self):
        self._triggered = True
        self.setStyleSheet(
            f'#quoteCard {{'
            f'  background: {COLOR_TRIGGERED};'
            f'  border: 1px solid {COLOR_TRIGGERED};'
            f'  border-radius: 6px;'
            f'}}'
        )

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
            self.name_label.setToolTip(name)
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

        # 状态显示
        if triggered:
            self.status_label.setText(f'●{trigger_reason}')
            self.status_label.setStyleSheet(
                f'color: white; font-size: 9px; font-weight: 600;'
            )
        elif target is not None or target_pct is not None:
            parts = []
            if target is not None:
                parts.append(f'{DIR_LABELS.get(direction, direction)}{target}')
            if target_pct is not None:
                pct_dir = data.get('pct_dir', 'both')
                parts.append(f'{PCT_DIR_LABELS.get(pct_dir, pct_dir)}{abs(target_pct)}%')
            self.status_label.setText(' '.join(parts))
            self.status_label.setStyleSheet(
                f'color: {COLOR_TEXT_DIM}; font-size: 9px;'
            )
        else:
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
                f'color: {COLOR_TEXT_DIM}; font-size: 9px;'
                f'border: 1px solid {COLOR_BORDER}; border-radius: 2px;'
            )



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

    def _build_ui(self):
        self.setWindowTitle('监控设置')
        self.setMinimumSize(720, 520)
        self.setStyleSheet(
            f'QDialog {{ background: {COLOR_BG}; color: {COLOR_TEXT}; }}'
            f'QLabel {{ color: {COLOR_TEXT}; font-size: 12px; }}'
            f'QLineEdit, QComboBox, QSpinBox {{'
            f'  background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT};'
            f'  border: 1px solid {COLOR_BORDER}; border-radius: 4px;'
            f'  padding: 4px 6px; font-size: 12px;'
            f'}}'
            f'QLineEdit:focus, QComboBox:focus {{ border-color: {COLOR_DOWN}; }}'
            f'QComboBox QAbstractItemView {{'
            f'  background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT};'
            f'  border: 1px solid {COLOR_BORDER};'
            f'  selection-background-color: {COLOR_BORDER}; selection-color: {COLOR_TEXT};'
            f'  outline: none; padding: 2px; font-size: 12px;'
            f'}}'
            f'QComboBox::drop-down {{ border: none; width: 20px; }}'
            f'QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;'
            f'  border-right: 4px solid transparent; border-top: 5px solid {COLOR_TEXT_DIM}; }}'
            f'QSpinBox::up-button, QSpinBox::down-button {{'
            f'  background: {COLOR_BG_LIGHT}; border: none; width: 16px; }}'
            f'QSpinBox::up-arrow {{ border-left: 3px solid transparent;'
            f'  border-right: 3px solid transparent; border-bottom: 4px solid {COLOR_TEXT_DIM}; }}'
            f'QSpinBox::down-arrow {{ border-left: 3px solid transparent;'
            f'  border-right: 3px solid transparent; border-top: 4px solid {COLOR_TEXT_DIM}; }}'
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 全局设置
        top_box = QGroupBox('全局设置')
        top_box.setStyleSheet(
            f'QGroupBox {{ color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER};'
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

        self.source_combo = QComboBox()
        self.source_combo.addItems(['auto', 'tencent', 'yahoo', 'tdx'])
        self.source_combo.setCurrentText(self.config.get('source', 'auto'))
        top_form.addRow('数据源:', self.source_combo)
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
            f'QTableWidget {{ gridline-color: {COLOR_BORDER};'
            f'  background: {COLOR_BG}; alternate-background-color: {COLOR_BG_LIGHT};'
            f'  border: 1px solid {COLOR_BORDER}; border-radius: 6px;'
            f'}}'
            f'QTableWidget::item {{ padding: 4px; }}'
            f'QHeaderView::section {{'
            f'  background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT};'
            f'  padding: 4px; border: none; border-right: 1px solid {COLOR_BORDER};'
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

    def _set_cell(self, row, col, value):
        item = QTableWidgetItem(str(value))
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor(COLOR_TEXT))
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
            f'QComboBox {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT};'
            f'  border: 1px solid {COLOR_BORDER}; border-radius: 3px; padding: 2px 4px;'
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
        self.config['source'] = self.source_combo.currentText()
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

        self._init_window()
        self._init_ui()
        self._init_tray()
        self._move_to_bottom_right()

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
            f'  background: {COLOR_BG};'
            f'  border-radius: 10px;'
            f'  border: 1px solid {COLOR_BORDER};'
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
            f'QScrollBar:vertical {{ background: {COLOR_BG_LIGHT}; width: 6px; margin: 0; }}'
            f'QScrollBar::handle:vertical {{ background: {COLOR_BORDER}; border-radius: 3px; min-height: 20px; }}'
            f'QScrollBar::handle:vertical:hover {{ background: {COLOR_TEXT_DIM}; }}'
            f'QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; }}'
            f'QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}'
            # 水平滚动条：高度设为0，彻底不占空间
            f'QScrollBar:horizontal {{ height: 0; width: 0; background: transparent; }}'
            f'QScrollBar::handle:horizontal {{ background: transparent; }}'
            f'QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ height: 0; width: 0; }}'
        )

        self.list_widget = QWidget()
        self.list_widget.setStyleSheet(f'QWidget {{ background: {COLOR_BG}; }}')
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
            f'background: transparent; border-top: 1px solid {COLOR_BORDER};'
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 0, 6, 0)
        h.setSpacing(4)

        # 左侧：状态点 + 更新时间 + 运行时长
        self.status_dot = QLabel('●')
        self.status_dot.setStyleSheet(f'color: {COLOR_FLAT}; font-size: 9px;')
        h.addWidget(self.status_dot)

        self.update_label = QLabel('--:--')
        self.update_label.setStyleSheet(f'color: {COLOR_TEXT_DIM}; font-size: 9px;')
        h.addWidget(self.update_label)

        self.uptime_label = QLabel('')
        self.uptime_label.setStyleSheet(f'color: {COLOR_TEXT_DIM}; font-size: 9px;')
        h.addWidget(self.uptime_label)

        h.addStretch()

        # 右侧：设置（保存时自动刷新配置）
        self.btn_settings = QPushButton('⚙')
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setFixedSize(24, 20)
        self.btn_settings.setStyleSheet(
            f'QPushButton {{ background: {COLOR_BG_LIGHT}; color: {COLOR_TEXT};'
            f'  border: 1px solid {COLOR_BORDER}; border-radius: 4px; font-size: 11px; }}'
            f'QPushButton:hover {{ background: {COLOR_BORDER}; }}'
        )
        self.btn_settings.clicked.connect(self._open_settings)
        h.addWidget(self.btn_settings)

        parent_layout.addWidget(bar)

    def _init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        pix = self._make_tray_icon()
        self.tray.setIcon(QIcon(pix))
        self.tray.setToolTip(f'Stock Monitor v{VERSION}')

        menu = QMenu()
        act_show = QAction('显示悬浮窗', self)
        act_show.triggered.connect(self.show_normal)
        act_quit = QAction('退出', self)
        act_quit.triggered.connect(QApplication.quit)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _make_tray_icon(self):
        from PySide6.QtGui import QPixmap
        pix = QPixmap(16, 16)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(COLOR_DOWN)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, 16, 16, 4, 4)
        p.setPen(QPen(QColor('white')))
        p.setFont(QFont('Segoe UI', 8, QFont.Bold))
        p.drawText(pix.rect(), Qt.AlignCenter, 'S')
        p.end()
        return pix

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_normal()

    def show_normal(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

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

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        dlg.config_changed.connect(self._on_config_changed)
        dlg.exec()

    def _on_config_changed(self, new_config):
        self.config = new_config
        was_running = self.worker and self.worker.isRunning()
        if was_running:
            self.worker.stop()
            self.worker.wait(2000)
        self._rebuild_cards()
        if was_running:
            self.start_monitor()

    def show_kline_for_card(self, code, name, mouse_pos):
        """Show kline tooltip instantly from cache (zero latency)"""
        klines = []
        if self.worker and hasattr(self.worker, 'kline_cache'):
            cached = self.worker.kline_cache.get(code)
            if cached:
                klines = cached[1]
        if klines:
            self._show_kline_tooltip(code, name, klines, mouse_pos)

    def _show_kline_tooltip(self, code, name, klines, mouse_pos):
        """显示K线悬浮图, 智能定位"""
        if self._kline_tooltip is None:
            self._kline_tooltip = KlineTooltip()

        self._kline_tooltip.set_data(code, name, klines)

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
        msg = data.get('message', '')
        if hasattr(self, 'tray') and self.tray.isVisible():
            self.tray.showMessage(
                '价格预警', msg,
                QSystemTrayIcon.Warning, 5000
            )

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

    def hideEvent(self, event):
        if hasattr(self, 'tray') and self.tray.isVisible():
            event.ignore()
            self.hide()
        else:
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
