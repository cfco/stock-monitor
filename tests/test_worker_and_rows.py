"""P0 回归：取数线程不能静默死掉 + 设置表格删行按按钮定位真实行"""
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QPushButton, QTableWidget, QTableWidgetItem

import floating_window as fw


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def worker(monkeypatch):
    w = fw.QuoteWorker({'interval': 1, 'alerts': [], 'source': 'auto'})
    monkeypatch.setattr(fw.time, 'sleep', lambda *_a, **_k: None)   # 免等重试间隔
    return w


def test_round_exception_does_not_kill_worker_silently(worker):
    """异常让 run() 提前返回时，原实现连 'stopped' 都发不出去 → 状态点仍是绿色。
    现在必须显式报 error，并把 running 置 False。"""
    statuses = []
    worker.status_changed.connect(statuses.append)
    worker._run_loop = lambda: (_ for _ in ()).throw(ValueError('坏配置'))

    worker.run()

    assert statuses == ['running', 'error']
    assert worker.running is False


def test_normal_stop_reports_stopped(worker):
    statuses = []
    worker.status_changed.connect(statuses.append)

    def loop():
        worker.running = False          # 模拟 stop() 后的正常退出

    worker._run_loop = loop
    worker.run()

    assert statuses == ['running', 'stopped']


def test_transient_failure_is_retried_before_giving_up(worker):
    """前两轮异常、第三轮正常退出：不应报 error（偶发网络/解析问题不该终止线程）"""
    statuses = []
    worker.status_changed.connect(statuses.append)
    calls = []

    def loop():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError('偶发')
        worker.running = False

    worker._run_loop = loop
    worker.run()

    assert len(calls) == 3
    assert statuses == ['running', 'stopped']


def test_delete_row_uses_the_buttons_current_row(qapp):
    """删行必须按按钮「当前」所在行：捕获创建时的行号会在 removeRow 后删错行"""
    table = QTableWidget(3, 9)
    codes = ['sh601398', 'sh600000', 'sz000988']
    buttons = []
    for row, code in enumerate(codes):
        table.setItem(row, 0, QTableWidgetItem(code))
        btn = QPushButton('x')
        buttons.append(btn)
        table.setCellWidget(row, 8, btn)

    dialog = types.SimpleNamespace(table=table)
    # 先删中间一行：原本第 3 行的按钮随之上移到第 2 行（索引 1）。
    # 旧实现仍用创建时的行号 2 → removeRow(2) 越界，中间那只被删、后一只删不掉。
    fw.SettingsDialog._delete_row(dialog, buttons[1])
    assert table.rowCount() == 2
    fw.SettingsDialog._delete_row(dialog, buttons[2])

    assert table.rowCount() == 1
    assert table.item(0, 0).text() == 'sh601398'     # 剩下的必须是没被点过的那只


def test_restart_waits_for_busy_old_worker(qapp):
    """保存设置时旧线程若还卡在一次取数里：必须等它真退出再建新的。

    旧实现 wait(2000) 超时后照样调用 start_monitor()，而那里的 isRunning() 守卫
    会直接 return → 新线程永远建不起来，行情从此永久停更。
    """
    win = fw.FloatingWindow.__new__(fw.FloatingWindow)      # 只测重启决策，不建整套 UI
    win.config = {'theme': 'dark', 'alerts': [], 'interval': 1}
    win._theme_mode = 'dark'                                # 主题不变 → 跳过换肤分支
    win._rebuild_cards = lambda: None
    starts = []
    win.start_monitor = lambda: starts.append(win.worker.isRunning())

    old = fw.QuoteWorker({'interval': 1, 'alerts': []})
    gate = threading.Event()
    old._run_loop = lambda: gate.wait(5)                    # 模拟卡在网络取数
    old.start()
    win.worker = old

    fw.FloatingWindow._on_config_changed(win, {'theme': 'dark', 'alerts': [], 'interval': 1})
    assert starts == []                                     # wait 超时 → 不该当场重启

    gate.set()
    assert old.wait(3000)
    deadline = time.time() + 3
    while not starts and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert starts == [False]                                # 旧线程退出后才重启一次
    old.deleteLater()


def _quiet_worker(worker, monkeypatch):
    """跑一轮 _run_loop 但不让 K 线预取线程真去联网"""
    worker.running = True
    monkeypatch.setattr(fw.time, 'sleep', lambda *_a, **_k: None)
    for name in ('_fetch_a_share_klines', '_fetch_us_klines', '_fetch_kline_period'):
        monkeypatch.setattr(worker, name, lambda *a, **k: None)
    monkeypatch.setattr(fw.alpaca_history, 'in_us_data_window', lambda *a: False)
    return worker


def _us_config():
    return {'interval': 1, 'source': 'auto', 'alpaca': {'enabled': True}, 'alerts': [
        {'code': 'aapl', 'name': '苹果'}, {'code': 'usTSLA', 'name': '特斯拉'}]}


def test_us_results_map_back_to_the_configured_code(qapp, monkeypatch):
    """配置里写 'aapl' / 'usTSLA'：按归一化代码请求，结果必须回挂到原样代码上。
    旧实现用原样代码去查大写键，卡片会永远显示 '--'。"""
    worker = _quiet_worker(fw.QuoteWorker({'interval': 1, 'source': 'auto', 'alerts': []}), monkeypatch)
    seen = {}

    def fake_a(codes, source='auto'):
        seen['a'] = list(codes)
        return {}

    def fake_us(codes):
        seen['us'] = list(codes)
        return {c: {'price': 10.0, 'yest_close': 9.0, 'change_pct': 11.1} for c in codes}

    monkeypatch.setattr(fw, 'get_quotes_batch', fake_a)
    monkeypatch.setattr(fw.alpaca_stream, 'get_quotes_batch', fake_us)
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    worker.config = _us_config()

    payloads = []

    def on_payload(p):
        payloads.append(p)
        if len(payloads) >= 2:
            worker.running = False

    worker.quote_updated.connect(on_payload)
    worker.batch_done.connect(lambda _ts: setattr(worker, 'running', False))   # 兜底防死循环
    worker._run_loop()

    assert seen['us'] == ['AAPL', 'TSLA']
    assert {p['code']: p['price'] for p in payloads} == {'aapl': 10.0, 'usTSLA': 10.0}


def test_us_disabled_skips_alpaca_entirely(qapp, monkeypatch):
    """取消勾选「启用美股」后：REST 也不该再打 Alpaca（原先只是停 WebSocket）"""
    worker = _quiet_worker(fw.QuoteWorker({'interval': 1, 'source': 'auto', 'alerts': []}), monkeypatch)
    calls = []
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': {})
    monkeypatch.setattr(fw.alpaca_stream, 'get_quotes_batch',
                        lambda codes: calls.append(list(codes)) or {})
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    config = _us_config()
    config['alpaca']['enabled'] = False
    worker.config = config

    worker.batch_done.connect(lambda _ts: setattr(worker, 'running', False))
    worker._run_loop()

    assert calls == []


def test_off_hours_round_is_not_counted_as_error(qapp, monkeypatch):
    """纯 A 股列表收盘后没有任何可取的行情，这不算失败：
    旧实现每 10 轮误报一次红点，还反复重建 TDX 连接"""
    worker = _quiet_worker(fw.QuoteWorker({'interval': 1, 'source': 'auto', 'alerts': [
        {'code': 'sh600000', 'name': '浦发银行'}]}), monkeypatch)
    worker._first_loop = False
    calls = []
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': calls.append(1) or {})
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: False)

    statuses = []
    worker.status_changed.connect(statuses.append)
    rounds = []

    def on_batch(_ts):
        rounds.append(1)
        if len(rounds) >= 12:
            worker.running = False

    worker.batch_done.connect(on_batch)
    worker._run_loop()

    assert calls == []
    assert len(rounds) >= 12
    assert 'error' not in statuses


def test_resolve_callback_writes_to_the_row_that_asked(qapp):
    """代码查询回调必须写回「发起查询的那一行」：从 self._resolve_thread 反查会串到别的行"""
    table = QTableWidget(2, 9)
    item_a = QTableWidgetItem('')
    item_b = QTableWidgetItem('')
    table.setItem(0, 0, item_a)
    table.setItem(1, 0, item_b)
    dialog = types.SimpleNamespace(
        table=table, _block_signals=False,
        _resolve_thread=types.SimpleNamespace(_item=item_b, _name_item=None))
    thread_a = types.SimpleNamespace(_item=item_a, _name_item=None)

    fw.SettingsDialog._on_resolve_done(dialog, [('sh600000', '浦发银行')], thread_a)

    assert item_a.text() == 'sh600000'
    assert item_b.text() == ''          # 后发起的那一行不能被前一个查询的结果污染
