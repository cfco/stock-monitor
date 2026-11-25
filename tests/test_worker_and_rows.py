"""P0 回归：取数线程不能静默死掉 + 设置表格删行按按钮定位真实行"""
import os
import sys
import datetime
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
        _resolve_generations={id(item_a): 2},
        _resolve_thread=types.SimpleNamespace(_item=item_b, _name_item=None))
    thread_a = types.SimpleNamespace(
        _item=item_a, _name_item=None, _row=0, _generation=2,
        _request_key=id(item_a))

    fw.SettingsDialog._on_resolve_done(dialog, [('sh600000', '浦发银行')], thread_a)

    assert item_a.text() == 'sh600000'
    assert item_b.text() == ''          # 后发起的那一行不能被前一个查询的结果污染


def _fake_quote_sequence(prices, change_pcts=None):
    """返回一个按调用次数依次吐出价格序列的 get_quotes_batch 替身。"""
    seq = list(prices)
    pcts = list(change_pcts or [None] * len(seq))
    state = {'idx': 0}

    def fake(codes, source='auto'):
        idx = min(state['idx'], len(seq) - 1)
        quote = {'price': seq[idx], 'yest_close': 100.0}
        if pcts[idx] is not None:
            quote['change_pct'] = pcts[idx]
        state['idx'] += 1
        return {c: dict(quote) for c in codes}

    return fake


def test_price_alert_is_one_shot_until_condition_resets(qapp, monkeypatch):
    """条件持续满足只提醒一次；价格回到安全侧后再跌破才重新提醒。"""
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行', 'target': 10.0, 'dir': 'below'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch',
                        _fake_quote_sequence([9.0, 9.0, 11.0, 9.0]))

    alerts = []
    worker.alert_triggered.connect(alerts.append)
    rounds = []
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 4))
    worker._run_loop()

    assert [a['direction'] for a in alerts] == ['below', 'below']
    assert '跌破' in alerts[0]['message']


def test_pct_alert_is_one_shot_per_direction(qapp, monkeypatch):
    """涨跌幅预警按方向各自锁存：先跌触发一次，再涨触发一次，不重复刷屏。"""
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行',
             'target_pct': 3, 'pct_dir': 'both'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch',
                        _fake_quote_sequence(
                            [10.0] * 4, [-4.0, -4.0, 4.0, -4.0]))

    alerts = []
    worker.alert_triggered.connect(alerts.append)
    rounds = []
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 4))
    worker._run_loop()

    assert [a['direction'] for a in alerts] == ['below', 'above', 'below']


def test_off_hours_startup_suppresses_alerts(qapp, monkeypatch):
    """非交易时段启动拉一次收盘价可以显示，但不能拿隔夜旧价触发预警。"""
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行', 'target': 10.0, 'dir': 'below'},
        ],
    }), monkeypatch)
    worker._first_loop = True
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: False)
    monkeypatch.setattr(fw, 'get_quotes_batch',
                        _fake_quote_sequence([9.0]))

    alerts = []
    payloads = []
    worker.alert_triggered.connect(alerts.append)
    worker.quote_updated.connect(payloads.append)
    rounds = []
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 1))
    worker._run_loop()

    assert alerts == []
    assert payloads[0]['stale'] is True


def test_status_recovers_after_consecutive_errors(qapp, monkeypatch):
    """连续失败亮红点后，下一轮成功必须恢复绿色（running），不能一直红着。"""
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    worker._last_status = 'running'   # 真实启动由 run() 先 emit 一次 running；这里直接调 _run_loop 要补上
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)

    calls = {'n': 0}

    def fake_quotes(codes, source='auto'):
        calls['n'] += 1
        if calls['n'] <= 10:
            return {}
        return {c: {'price': 10.0, 'change_pct': 1.0} for c in codes}

    monkeypatch.setattr(fw, 'get_quotes_batch', fake_quotes)
    statuses = []
    worker.status_changed.connect(statuses.append)
    rounds = []
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 11))
    worker._run_loop()

    assert statuses == ['error', 'running']


def test_off_hours_reports_closed_not_running(qapp, monkeypatch):
    """休市时状态点必须是独立的 closed 档，不能继续亮「正常绿」。

    两者界面表现完全一致（数字都不动），用户分不清是没到交易时间还是取数卡死。
    """
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    worker._last_status = 'running'
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: False)
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda *a, **k: {})

    statuses = []
    worker.status_changed.connect(statuses.append)
    worker.batch_done.connect(lambda _ts: setattr(worker, 'running', False))
    worker._run_loop()

    assert statuses == ['closed']


def test_status_dot_distinguishes_closed_from_error(qapp):
    """休市蓝 / 故障红 / 停止灰 / 正常绿 四档颜色必须互不相同且带说明。"""
    win = fw.FloatingWindow.__new__(fw.FloatingWindow)
    win.status_dot = fw.QLabel()
    seen = {}
    for status in ('running', 'closed', 'stopped', 'error'):
        fw.FloatingWindow.on_status(win, status)
        seen[status] = (win.status_dot.styleSheet(), win.status_dot.toolTip())

    colors = {s: sheet for s, (sheet, _tip) in seen.items()}
    assert len(set(colors.values())) == 4, '四档状态点颜色必须可区分'
    assert all(tip for _sheet, tip in seen.values()), '每档都要有悬停说明'
    assert seen['closed'][0] == f'color: {fw.COLOR_CLOSED}; font-size: 9px;'


def test_target_columns_reject_non_numeric_input(qapp):
    """目标价/目标% 在输入层就挡掉非数字，不用等保存才报错。

    空单元格必须继续放行——它表示「这条不设预警」，不是非法值。
    """
    delegate = fw._NumericColumnDelegate()
    v = delegate.validator
    assert v.validate('7.10', 0)[0] == fw.QDoubleValidator.Acceptable
    assert v.validate('14:58:03', 0)[0] == fw.QDoubleValidator.Invalid
    assert v.validate('abc', 0)[0] == fw.QDoubleValidator.Invalid
    assert v.validate('', 0)[0] == fw.QDoubleValidator.Intermediate

    dialog = fw.SettingsDialog({'interval': 5, 'alerts': []})
    assert isinstance(dialog.table.itemDelegateForColumn(2), fw._NumericColumnDelegate)
    assert isinstance(dialog.table.itemDelegateForColumn(4), fw._NumericColumnDelegate)
    # 代码/名称/备注列不能被数字校验器管着
    assert not isinstance(dialog.table.itemDelegateForColumn(0), fw._NumericColumnDelegate)
    assert not isinstance(dialog.table.itemDelegateForColumn(7), fw._NumericColumnDelegate)
    dialog.deleteLater()


def test_kline_change_pct_uses_prev_close_not_first_open(qapp):
    """悬浮图涨跌幅基准：分时用昨收，日/周 K 用上一根收盘，不能拿首根开盘凑数。"""
    tip = fw.KlineTooltip()
    tip.code = 'sh600000'
    tip.period = '1min'
    tip.ref_close = 100.0
    tip.klines = [
        {'open': 90.0, 'close': 97.0},
        {'open': 97.0, 'close': 110.0},
    ]
    assert tip._change_pct() == pytest.approx(10.0)

    # 没有昨收时才退到上一根收盘：不是首根开盘 90 → (99-90)/90 = 10%
    tip.ref_close = None
    tip.klines = [
        {'open': 90.0, 'close': 95.0},
        {'open': 95.0, 'close': 99.0},
    ]
    assert tip._change_pct() == pytest.approx((99 - 95) / 95 * 100)

    # 日K/周K 忽略分时的昨收字段
    tip.period = 'daily'
    tip.ref_close = 50.0
    assert tip._change_pct() == pytest.approx((99 - 95) / 95 * 100)
    assert tip._price_fmt(1.23456) == '1.23'
    tip.code = 'sh510300'
    assert tip._price_fmt(1.23456) == '1.235'
    tip.close()


def test_save_config_restarts_a_dead_fetch_thread():
    """取数线程已死/从未启动时保存设置：必须立刻重建线程。

    原实现只在 old.isRunning() 为真时重启，红点状态下改完设置只重排卡片，
    界面呈现「新股票列表 + 旧行情」，要等 5 秒自愈定时器才恢复。
    """
    win = fw.FloatingWindow.__new__(fw.FloatingWindow)
    win.config = {'theme': 'dark', 'alerts': [], 'interval': 1}
    win._theme_mode = 'dark'
    win._shutting_down = False
    win._rebuild_cards = lambda: None
    win.worker = None
    starts = []
    win.start_monitor = lambda: starts.append(1)

    fw.FloatingWindow._on_config_changed(win, {'theme': 'dark', 'alerts': [], 'interval': 1})

    assert starts == [1]


def test_finished_worker_object_still_gets_replaced():
    """worker 引用还在但线程已结束（非 fatal 退出，无自愈定时器）：同样要重启。

    这种情况下没有任何路径会再调 start_monitor，行情会永久停在旧值。
    """
    win = fw.FloatingWindow.__new__(fw.FloatingWindow)
    win.config = {'theme': 'dark', 'alerts': [], 'interval': 1}
    win._theme_mode = 'dark'
    win._shutting_down = False
    win._rebuild_cards = lambda: None

    dead = fw.QuoteWorker({'interval': 1, 'alerts': []})   # 构造后未 start，isRunning() False
    win.worker = dead
    starts = []
    win.start_monitor = lambda: starts.append(1)

    fw.FloatingWindow._on_config_changed(win, {'theme': 'dark', 'alerts': [], 'interval': 1})

    assert starts == [1]


def test_card_time_label_stays_pinned_right(qapp):
    """删掉恒空的 status_label 后，更新时间仍必须贴在卡片右缘。

    status_label 原本是这一行唯一带 stretch 的项，直接删会让时间标签紧贴涨跌幅、
    右侧空出一坨。改用 addStretch(1) 顶住，几何位置必须和删之前一致。
    """
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({'price': 7.1, 'change_pct': 0.5, 'last_update': '14:58:03'})
    card.resize(300, 24)
    card.show()
    qapp.processEvents()

    assert not hasattr(card, 'status_label')
    assert card.time_label.x() + card.time_label.width() > card.width() - 12
    # 常量说窗口 292 才放得下时间列：行最小宽 + 20px 窗口装饰必须正好等于它，
    # 否则以后改任一列宽，隐藏阈值就和真实占位脱节了
    assert card.layout().minimumSize().width() + 20 == fw.CARD_TIME_MIN_WINDOW_WIDTH
    card.close()


def test_card_time_shows_hh_mm_only(qapp):
    """40px 塞不下 HH:MM:SS；卡片只走时:分，完整秒数留在 tooltip 和底栏。"""
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({'price': 7.1, 'change_pct': 0.5, 'last_update': '14:58:03'})
    assert card.time_label.text() == '14:58'
    assert '14:58:03' in card.time_label.toolTip()


def test_narrow_window_hides_time_column(qapp):
    """窗口放不下时整列隐藏，而不是裁成半截（水平滚动条是关掉的）。"""
    win = fw.FloatingWindow.__new__(fw.FloatingWindow)
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({'price': 7.1, 'change_pct': 0.5, 'last_update': '14:58:03'})
    win.cards = {'sh601398': card}

    win.width = lambda: 260
    fw.FloatingWindow._apply_time_column_visibility(win)
    assert card.time_label.isVisibleTo(card) is False

    win.width = lambda: 300
    fw.FloatingWindow._apply_time_column_visibility(win)
    assert card.time_label.isVisibleTo(card) is True


def test_trigger_switches_card_style_without_status_text(qapp):
    """触发态只由绿底表达：清掉 trigger_reason 不能改变这个行为。"""
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({'price': 6.5, 'change_pct': -1.0,
                   'target': 7.0, 'direction': 'below'})
    assert card._triggered is True

    card.set_data({'price': 7.5, 'change_pct': 1.0,
                   'target': 7.0, 'direction': 'below'})
    assert card._triggered is False

    card.set_data({'price': 7.5, 'change_pct': 4.0,
                   'target_pct': 3.0, 'pct_dir': 'above'})
    assert card._triggered is True


def test_stale_card_grays_price_and_shows_position_note(qapp):
    """非实时价要一眼能认出来：收盘价置灰，持仓/备注悬停可见。

    position/note 之前只在配置与 payload 里流转，界面从不显示。
    """
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({
        'price': 7.1, 'change_pct': 0.5, 'stale': True,
        'position': '1000股', 'note': '长线持有', 'last_update': '15:00:00',
    })
    assert fw.COLOR_FLAT in card.price_label.styleSheet()
    tooltip = card.name_label.toolTip()
    assert '工商银行' in tooltip
    assert '持仓: 1000股' in tooltip
    assert '备注: 长线持有' in tooltip

    card.set_data({
        'price': 7.2, 'change_pct': 0.5, 'stale': False,
        'position': '1000股', 'last_update': '15:00:00',
    })
    assert fw.COLOR_FLAT not in card.price_label.styleSheet()
    assert '备注' not in card.name_label.toolTip()


def test_stale_resolve_callback_does_not_overwrite_new_input(qapp):
    table = QTableWidget(1, 2)
    item = QTableWidgetItem('sh601398')
    name = QTableWidgetItem('工商银行')
    table.setItem(0, 0, item)
    table.setItem(0, 1, name)
    dialog = types.SimpleNamespace(
        table=table, _block_signals=False,
        _resolve_generations={id(item): 3})
    old_thread = types.SimpleNamespace(
        _item=item, _name_item=name, _row=0, _generation=2,
        _request_key=id(item))

    fw.SettingsDialog._on_resolve_done(
        dialog, [('sh600000', '浦发银行')], old_thread)

    assert item.text() == 'sh601398'
    assert name.text() == '工商银行'


def test_clearing_cell_invalidates_pending_resolve(qapp):
    table = QTableWidget(1, 2)
    item = QTableWidgetItem('sh600000')
    table.setItem(0, 0, item)
    dialog = types.SimpleNamespace(
        table=table, _block_signals=False, _resolve_generations={id(item): 1})
    thread = types.SimpleNamespace(
        _item=item, _name_item=None, _row=0, _generation=1,
        _request_key=id(item))
    item.setText('')

    fw.SettingsDialog._on_code_changed(dialog, 0, 0)
    fw.SettingsDialog._on_resolve_done(dialog, [('sh600000', '浦发银行')], thread)

    assert item.text() == ''


def test_resolve_generations_are_independent_per_cell(qapp):
    table = QTableWidget(2, 2)
    item_a, item_b = QTableWidgetItem(''), QTableWidgetItem('')
    table.setItem(0, 0, item_a)
    table.setItem(1, 0, item_b)
    dialog = types.SimpleNamespace(
        table=table, _block_signals=False,
        _resolve_generations={id(item_a): 1, id(item_b): 2})
    thread_a = types.SimpleNamespace(
        _item=item_a, _name_item=None, _row=0, _generation=1,
        _request_key=id(item_a))
    thread_b = types.SimpleNamespace(
        _item=item_b, _name_item=None, _row=1, _generation=2,
        _request_key=id(item_b))

    fw.SettingsDialog._on_resolve_done(dialog, [('sh600000', '浦发银行')], thread_a)
    fw.SettingsDialog._on_resolve_done(dialog, [('sh601398', '工商银行')], thread_b)

    assert item_a.text() == 'sh600000'
    assert item_b.text() == 'sh601398'


def test_resolve_callback_ignores_deleted_row(qapp):
    table = QTableWidget(1, 2)
    item = QTableWidgetItem('sh600000')
    table.setItem(0, 0, item)
    dialog = types.SimpleNamespace(
        table=table, _block_signals=False,
        _resolve_generations={id(item): 1})
    thread = types.SimpleNamespace(
        _item=item, _name_item=None, _row=0, _generation=1,
        _request_key=id(item))
    table.removeRow(0)

    fw.SettingsDialog._on_resolve_done(dialog, [('sh600000', '浦发银行')], thread)
    assert table.rowCount() == 0


def test_stale_a_share_quote_does_not_trigger_alert(qapp, monkeypatch):
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行', 'target': 10.0, 'dir': 'below'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': {
        'sh600000': {'price': 9.0, 'change_pct': -1.0,
                     'quote_ts': time.time() - 3600},
    })
    alerts, payloads, rounds = [], [], []
    worker.alert_triggered.connect(alerts.append)
    worker.quote_updated.connect(payloads.append)
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 1))

    worker._run_loop()

    assert alerts == []
    assert payloads[0]['stale'] is True
    assert payloads[0]['last_update'] != '--'


def test_us_off_hours_quote_does_not_trigger_alert(qapp, monkeypatch):
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alpaca': {'enabled': True}, 'alerts': [
            {'code': 'AAPL', 'name': '苹果', 'target': 200.0, 'dir': 'below'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    monkeypatch.setattr(fw.alpaca_history, 'in_us_data_window', lambda *a: False)
    monkeypatch.setattr(fw.alpaca_stream, 'get_quotes_batch', lambda codes: {
        'AAPL': {'price': 190.0, 'change_pct': -1.0, 'stale': True},
    })
    alerts, payloads, rounds = [], [], []
    worker.alert_triggered.connect(alerts.append)
    worker.quote_updated.connect(payloads.append)
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 1))

    worker._run_loop()

    assert alerts == []
    assert payloads[0]['stale'] is True


def test_us_without_credentials_does_not_mask_a_share_success(qapp, monkeypatch):
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alpaca': {'enabled': True}, 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行'},
            {'code': 'AAPL', 'name': '苹果'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    worker._last_status = 'running'
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw.alpaca_history, 'has_credentials', lambda: False)
    monkeypatch.setattr(fw.alpaca_history, 'in_us_data_window', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': {
        'sh600000': {'price': 10.0, 'change_pct': 1.0}})
    monkeypatch.setattr(fw.alpaca_stream, 'get_quotes_batch', lambda codes: {})
    statuses, rounds = [], []
    worker.status_changed.connect(statuses.append)
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 10))

    worker._run_loop()

    assert statuses == []


def test_us_success_does_not_mask_a_share_failure(qapp, monkeypatch):
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alpaca': {'enabled': True}, 'alerts': [
            {'code': 'sh600000', 'name': '浦发银行'},
            {'code': 'AAPL', 'name': '苹果'},
        ],
    }), monkeypatch)
    worker._first_loop = False
    worker._last_status = 'running'
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw.alpaca_history, 'has_credentials', lambda: True)
    monkeypatch.setattr(fw.alpaca_history, 'in_us_data_window', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': {})
    monkeypatch.setattr(fw.alpaca_stream, 'get_quotes_batch', lambda codes: {
        'AAPL': {'price': 190.0, 'change_pct': 1.0},
    })
    statuses, rounds = [], []
    worker.status_changed.connect(statuses.append)
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 10))

    worker._run_loop()

    assert statuses == ['error']


def test_quote_timestamp_is_shown_instead_of_fetch_time(qapp, monkeypatch):
    worker = _quiet_worker(fw.QuoteWorker({
        'interval': 1, 'source': 'auto', 'alerts': [{'code': 'sh600000'}],
    }), monkeypatch)
    worker._first_loop = False
    quote_ts = time.time() - 5
    expected = datetime.datetime.fromtimestamp(quote_ts).strftime('%H:%M:%S')
    monkeypatch.setattr(fw, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(fw, 'get_quotes_batch', lambda codes, source='auto': {
        'sh600000': {'price': 10.0, 'change_pct': 1.0, 'quote_ts': quote_ts},
    })
    payloads, rounds = [], []
    worker.quote_updated.connect(payloads.append)
    worker.batch_done.connect(lambda _ts: _stop_after(worker, rounds, 1))

    worker._run_loop()

    assert payloads[0]['last_update'] == expected


def test_kline_axis_contains_raw_extremes_and_centers_flat_price():
    low, high = 10.006, 10.034
    axis_min, axis_max, labels, divisions = fw.compute_kline_axis(
        high, low, high, 2)
    assert axis_min <= low < high <= axis_max
    assert len(labels) == divisions + 1

    axis_min, axis_max, labels, divisions = fw.compute_kline_axis(100, 100, 100, 2)
    assert axis_min < 100 < axis_max
    assert divisions == 5


def test_stale_card_does_not_show_triggered_style(qapp):
    card = fw.QuoteCard('sh601398', '工商银行')
    card.set_data({'price': 6.5, 'change_pct': -1.0,
                   'target': 7.0, 'direction': 'below'})
    assert card._triggered is True

    card.set_data({'price': 6.5, 'change_pct': -1.0, 'stale': True,
                   'target': 7.0, 'direction': 'below'})
    assert card._triggered is False


def test_settings_dialog_accepts_malformed_alpaca_section(qapp):
    dlg = fw.SettingsDialog({'interval': 5, 'alerts': [], 'alpaca': 'bad'})
    assert dlg.us_enable_check.isChecked() is False
    dlg.deleteLater()


def test_apply_alpaca_config_notifies_credentials_change(monkeypatch):
    calls = []
    win = types.SimpleNamespace(config={'alpaca': {'enabled': False}, 'alerts': []})
    monkeypatch.setattr(fw.secret_store, 'load_alpaca_creds', lambda alc: ('k', 's'))
    monkeypatch.setattr(fw.alpaca_history, 'configure', lambda k, s: calls.append(('configure', k, s)))
    monkeypatch.setattr(fw.alpaca_stream, 'credentials_changed',
                        lambda: calls.append(('credentials_changed',)))
    monkeypatch.setattr(fw.alpaca_stream, 'stop', lambda: calls.append(('stop',)))

    fw.FloatingWindow._apply_alpaca_config(win)

    assert calls == [('configure', 'k', 's'), ('credentials_changed',), ('stop',)]


def test_kline_batch_waits_for_daemon_workers_without_overlap(monkeypatch):
    worker = fw.QuoteWorker({'interval': 1, 'alerts': []})
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worker._us_kline_fetching = True

    def fetch(code, period, count):
        started.set()
        assert release.wait(2)
        return f'{code}_{period}', [{'time': '09:30', 'close': 10.0}]

    monkeypatch.setattr(fw.alpaca_history, 'get_kline', fetch)
    thread = threading.Thread(target=lambda: (
        worker._fetch_us_klines(['AAPL']), finished.set()), daemon=True)
    thread.start()
    assert started.wait(2)
    assert finished.is_set() is False
    assert worker._us_kline_fetching is True
    release.set()
    thread.join(2)

    assert finished.is_set() is True
    assert worker._us_kline_fetching is False
    assert worker._kline_get('AAPL')[1]


def test_kline_batch_timeout_releases_flag_and_next_batch_runs(monkeypatch):
    worker = fw.QuoteWorker({'interval': 1, 'alerts': []})
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worker._us_kline_fetching = True
    monkeypatch.setattr(fw, 'KLINE_FETCH_TIMEOUT', 0.05)

    def stuck(code, period, count):
        started.set()
        release.wait(2)
        return code, [{'time': '09:30', 'close': 10.0}]

    monkeypatch.setattr(fw.alpaca_history, 'get_kline', stuck)
    thread = threading.Thread(target=lambda: (
        worker._fetch_us_klines(['AAPL']), finished.set()), daemon=True)
    thread.start()
    assert started.wait(2)
    thread.join(2)
    assert finished.is_set() is True
    assert worker._us_kline_fetching is False

    worker._run_kline_batch(
        [('MSFT',)], lambda code: (code, [{'time': '09:30', 'close': 20.0}]), '测试')
    assert worker._kline_get('MSFT')[1]
    release.set()


def test_kline_queue_is_bounded_when_all_workers_are_stuck(monkeypatch):
    blocked_queue = fw.queue.Queue(maxsize=1)
    blocked_queue.put_nowait(('occupied',))
    monkeypatch.setattr(fw, '_kline_queue', blocked_queue)
    monkeypatch.setattr(fw, '_ensure_kline_workers', lambda: None)
    monkeypatch.setattr(fw, 'KLINE_FETCH_TIMEOUT', 0.05)
    worker = fw.QuoteWorker({'interval': 1, 'alerts': []})

    worker._run_kline_batch(
        [('AAPL',)], lambda code: (code, [{'close': 10.0}]), '测试')

    assert blocked_queue.maxsize == 1
    assert blocked_queue.qsize() == 1


def _stop_after(worker, rounds, limit):
    rounds.append(1)
    if len(rounds) >= limit:
        worker.running = False


class _FakeLibraryInfo:
    """把 QLibraryInfo 指到临时目录，造出"有/没有 xcb 插件"两种环境"""

    def __init__(self, root):
        self._root = str(root)

    def path(self, _lib):
        return self._root

    class LibraryPath:
        PluginsPath = None


def _fake_wayland_env(monkeypatch, **overrides):
    monkeypatch.delenv('QT_QPA_PLATFORM', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_wayland_falls_back_to_xcb_when_plugin_exists(monkeypatch, tmp_path):
    """Wayland + 有 xcb 插件 → 切到 xcb。

    xdg-shell 禁止客户端定位窗口，而"贴右下角"和"K 线图按鼠标弹出并避开主窗口"
    都只能靠绝对坐标，不切后端这两件事在 Wayland 下无解。
    """
    (tmp_path / 'platforms' / 'libqxcb.so').parent.mkdir(parents=True)
    (tmp_path / 'platforms' / 'libqxcb.so').write_bytes(b'')
    _fake_wayland_env(monkeypatch)
    monkeypatch.setattr('PySide6.QtCore.QLibraryInfo', _FakeLibraryInfo(tmp_path))

    fw._ensure_positionable_platform()

    assert os.environ['QT_QPA_PLATFORM'] == 'xcb'


def test_wayland_without_xcb_plugin_is_not_forced(monkeypatch, tmp_path):
    """精简安装没有 xcb 时不能强切，否则"位置不对"会变成"根本起不来"。"""
    (tmp_path / 'platforms').mkdir(parents=True)
    _fake_wayland_env(monkeypatch)
    monkeypatch.setattr('PySide6.QtCore.QLibraryInfo', _FakeLibraryInfo(tmp_path))

    fw._ensure_positionable_platform()

    assert 'QT_QPA_PLATFORM' not in os.environ


def test_explicit_platform_is_never_overwritten(monkeypatch, tmp_path):
    """用户自己设过 QT_QPA_PLATFORM 就尊重到底，哪怕正跑在 Wayland 上。"""
    (tmp_path / 'platforms' / 'libqxcb.so').parent.mkdir(parents=True)
    (tmp_path / 'platforms' / 'libqxcb.so').write_bytes(b'')
    _fake_wayland_env(monkeypatch, QT_QPA_PLATFORM='wayland')
    monkeypatch.setattr('PySide6.QtCore.QLibraryInfo', _FakeLibraryInfo(tmp_path))

    fw._ensure_positionable_platform()

    assert os.environ['QT_QPA_PLATFORM'] == 'wayland'


def test_x11_session_left_untouched(monkeypatch, tmp_path):
    """X11 会话本来就能自由定位，不该被改后端。"""
    (tmp_path / 'platforms' / 'libqxcb.so').parent.mkdir(parents=True)
    (tmp_path / 'platforms' / 'libqxcb.so').write_bytes(b'')
    _fake_wayland_env(monkeypatch, XDG_SESSION_TYPE='x11')
    monkeypatch.setattr('PySide6.QtCore.QLibraryInfo', _FakeLibraryInfo(tmp_path))

    fw._ensure_positionable_platform()

    assert 'QT_QPA_PLATFORM' not in os.environ
