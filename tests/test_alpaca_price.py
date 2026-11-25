"""现价口径回归：盘前/盘后的宽价差中值不能当现价（会显示假涨跌、误触目标价预警）

数值取自 2026-10-29 实测：ET 05:28（盘前）AAPL bp=325.91 / ap=360.46 / p=None，
昨收 347.10 —— 旧口径取中值 343.19，界面会显示 -1.13% 的假跌幅。
"""
import datetime
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import alpaca_history as H
import alpaca_stream as S


def test_last_trade_beats_mid():
    assert H.pick_quote_price(325.91, 360.46, 344.00, 347.10) == 344.0


def test_wide_spread_mid_is_rejected():
    assert H.pick_quote_price(325.91, 360.46, None, 347.10) is None


def test_narrow_spread_mid_is_accepted():
    assert H.pick_quote_price(347.00, 347.10, None, 347.10) == round((347.00 + 347.10) / 2, 4)


def test_single_sided_quote_still_accepted():
    """只有一条边时无法判价差宽窄，维持原行为（有真实挂单，好过没有）"""
    assert H.pick_quote_price(347.00, 0, None, 347.10) == 347.0


def test_without_reference_price_keeps_old_behaviour():
    assert H.pick_quote_price(10.0, 10.2, None, None) == 10.1


def test_bad_payloads_do_not_raise():
    assert H._quote_price(None) is None
    assert H._quote_price({'bp': 'x', 'ap': 'y'}) is None
    assert H.pick_quote_price(None, None, 'bad', 0) is None


def test_ws_wide_spread_quote_does_not_touch_cache():
    th = S._AlpacaStreamThread()
    H._set_prev_close('ZTEST', 347.10)
    th._handle_item({'T': 'q', 'sym': 'ZTEST', 'bp': 325.91, 'ap': 360.46,
                     't': '2026-10-29T09:28:00Z'})
    assert S._read_quote('ZTEST') is None            # 宽价差报价被丢弃

    th._handle_item({'T': 't', 'sym': 'ZTEST', 'p': 346.50,
                     't': '2026-10-29T09:28:01Z'})
    assert S._read_quote('ZTEST')['price'] == 346.5  # 成交价照常写入


def test_same_day_filter_drops_previous_session_bars():
    """盘前今日不足 count 根时，desc 取数会补进上一交易日的 bar，
    而分时标签只有 HH:MM → 图上出现两根同一时刻分属两天"""
    rows = [
        {'t': '2026-10-27T20:59:00Z', 'c': 1},    # 10-27 收盘前
        {'t': '2026-10-27T14:30:00Z', 'c': 1},    # 10-27 盘中
        {'t': '2026-10-26T19:00:00Z', 'c': 1},    # 上一交易日，必须被滤掉
    ]
    kept = H._same_day_bars(rows)
    assert len(kept) == 2
    assert all(r['t'].startswith('2026-10-27') for r in kept)


def test_bad_bar_does_not_take_down_the_whole_batch():
    """一只票的坏 bar 不能让整批美股当轮没行情（原先是 bars[1]['c'] 裸下标）"""
    # 固定 now_et：最新 bar 必须是「今天」，否则会走进盘前/休市口径，
    # 结果是 prev=latest，测试本身随运行时间漂移。
    now_et = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=H.TZ_ET)
    prev, latest = H._resolve_prev_close(
        [{'t': '2026-10-28T04:00:00Z', 'c': 10.0}, {'t': 'bad'}],
        now_et=now_et,
    )
    assert latest == 10.0 and prev is None
    prev, latest = H._resolve_prev_close([{'c': None}], now_et=now_et)
    assert (prev, latest) == (None, None)


@pytest.fixture
def snap_state(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(H, '_creds', {'key': 'k', 'secret': 's'})
    monkeypatch.setattr(H, '_snap_cache', {})
    monkeypatch.setattr(H, '_snap_ts', 0.0)
    monkeypatch.setattr(H, '_snap_fresh_ts', {})
    monkeypatch.setattr(H, '_snap_retry_after', {})
    monkeypatch.setattr(H, '_prev_close', {})
    monkeypatch.setattr(H, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(H, 'in_us_data_window', lambda *args: True)
    monkeypatch.setattr(H, '_resolve_prev_close', lambda bars, now_et=None: (100.0, 100.0))
    return clock


def _quote_at(clock, price):
    return {
        'p': price,
        't': datetime.datetime.fromtimestamp(
            clock[0], datetime.timezone.utc).isoformat(),
    }


def test_snapshot_failure_keeps_ttl_and_marks_old_price_stale(snap_state, monkeypatch):
    calls = []
    failing = [False]

    def daily(codes):
        calls.append(('daily', list(codes)))
        if failing[0]:
            return {}, set(codes)
        return {code: [{'c': 100.0}] for code in codes}, set()

    def quotes(codes):
        calls.append(('quotes', list(codes)))
        if failing[0]:
            return {}, set(codes)
        return {code: _quote_at(snap_state, 101.0 if snap_state[0] < 1100 else 102.0)
                for code in codes}, set()

    monkeypatch.setattr(H, '_fetch_daily_bars', daily)
    monkeypatch.setattr(H, '_fetch_latest_quotes', quotes)
    good = H.latest_snapshot_batch(['AAPL'])
    assert good['AAPL']['price'] == 101.0 and good['AAPL']['stale'] is False
    assert H._snap_ts == 1000.0

    failing[0] = True
    snap_state[0] = 1061.0
    failed = H.latest_snapshot_batch(['AAPL'], force=True)
    assert failed['AAPL']['price'] == 101.0
    assert failed['AAPL']['stale'] is True
    assert H._snap_ts == 1000.0                 # REST 失败没有刷新 TTL
    assert H._snap_fresh_ts['AAPL'] == 1000.0

    snap_state[0] = 1062.0
    assert H.latest_snapshot_batch(['AAPL'], force=True)['AAPL']['stale'] is True
    assert len(calls) == 4                       # 失败退避期内没有再次请求（force 也不绕过）

    failing[0] = False
    snap_state[0] = 1121.0
    recovered = H.latest_snapshot_batch(['AAPL'])
    assert recovered['AAPL']['price'] == 102.0
    assert recovered['AAPL']['stale'] is False


def test_partial_rest_failure_does_not_make_old_snapshot_fresh(snap_state, monkeypatch):
    quotes_fail = [False]

    def daily(codes):
        return {code: [{'c': 100.0}] for code in codes}, set()

    def quotes(codes):
        if quotes_fail[0]:
            return {}, set(codes)
        return {code: _quote_at(snap_state, 101.0) for code in codes}, set()

    monkeypatch.setattr(H, '_fetch_daily_bars', daily)
    monkeypatch.setattr(H, '_fetch_latest_quotes', quotes)
    H.latest_snapshot_batch(['AAPL'])

    quotes_fail[0] = True
    snap_state[0] = 1001.0
    result = H.latest_snapshot_batch(['AAPL'], force=True)['AAPL']
    assert result['price'] == 101.0 and result['stale'] is True
    assert H._snap_ts == 1000.0
    assert H._snap_fresh_ts['AAPL'] == 1000.0


def test_one_missing_symbol_does_not_punch_snapshot_ttl(snap_state, monkeypatch):
    monkeypatch.setattr(H, 'SNAPSHOT_TTL', 120)
    available = [{'AAPL'}]
    daily_calls = []

    def daily(codes):
        daily_calls.append(list(codes))
        return {code: [{'c': 100.0}] for code in codes if code in available[0]}, set()

    def quotes(codes):
        return {code: _quote_at(snap_state, 101.0) for code in codes
                if code in available[0]}, set()

    monkeypatch.setattr(H, '_fetch_daily_bars', daily)
    monkeypatch.setattr(H, '_fetch_latest_quotes', quotes)
    first = H.latest_snapshot_batch(['AAPL', 'MISSING'])
    assert first['AAPL']['stale'] is False and 'MISSING' not in first
    assert H._snap_ts == 0.0                     # 部分结果不冒充全量成功

    snap_state[0] = 1001.0
    H.latest_snapshot_batch(['AAPL', 'MISSING'])
    assert daily_calls == [['AAPL', 'MISSING']]  # 缺失项退避，AAPL 命中独立 TTL

    available[0].add('MISSING')
    snap_state[0] = 1060.0
    recovered = H.latest_snapshot_batch(['AAPL', 'MISSING'])
    assert daily_calls[-1] == ['MISSING']
    assert recovered['MISSING']['stale'] is False


def test_snapshot_off_hours_marks_close_price_stale(snap_state, monkeypatch):
    monkeypatch.setattr(H, 'in_us_data_window', lambda *args: False)
    monkeypatch.setattr(H, '_fetch_daily_bars', lambda codes: (
        {code: [{'c': 100.0}] for code in codes}, set()))
    monkeypatch.setattr(H, '_fetch_latest_quotes', lambda codes: (
        {code: _quote_at(snap_state, 101.0) for code in codes}, set()))

    result = H.latest_snapshot_batch(['AAPL'])['AAPL']

    assert result['price'] == 100.0
    assert result['stale'] is True


def test_snapshot_rejects_old_quote_event_during_window(snap_state, monkeypatch):
    monkeypatch.setattr(H, '_fetch_daily_bars', lambda codes: (
        {code: [{'c': 100.0}] for code in codes}, set()))
    old_time = datetime.datetime.fromtimestamp(
        snap_state[0] - H.SNAPSHOT_QUOTE_MAX_AGE - 1,
        datetime.timezone.utc).isoformat()
    monkeypatch.setattr(H, '_fetch_latest_quotes', lambda codes: (
        {code: {'p': 101.0, 't': old_time} for code in codes}, set()))

    result = H.latest_snapshot_batch(['AAPL'])['AAPL']

    assert result['price'] == 101.0
    assert result['stale'] is True


def test_missing_quote_enters_backoff_even_when_daily_bar_exists(snap_state, monkeypatch):
    daily_calls = []

    def daily(codes):
        daily_calls.append(list(codes))
        return {code: [{'c': 100.0}] for code in codes}, set()

    monkeypatch.setattr(H, '_fetch_daily_bars', daily)
    monkeypatch.setattr(H, '_fetch_latest_quotes', lambda codes: ({}, set()))

    assert H.latest_snapshot_batch(['AAPL'], force=True) == {}
    assert H._snap_retry_after['AAPL'] > snap_state[0]
    assert H.latest_snapshot_batch(['AAPL'], force=True) == {}
    assert daily_calls == [['AAPL']]


def test_malformed_quote_does_not_take_down_batch(snap_state, monkeypatch):
    monkeypatch.setattr(H, '_fetch_daily_bars', lambda codes: (
        {code: [{'c': 100.0}] for code in codes}, set()))
    monkeypatch.setattr(H, '_fetch_latest_quotes', lambda codes: ({
        'AAPL': 'malformed',
        'MSFT': _quote_at(snap_state, 101.0),
    }, set()))

    result = H.latest_snapshot_batch(['AAPL', 'MSFT'])

    assert 'AAPL' not in result
    assert result['MSFT']['price'] == 101.0
    assert H._snap_retry_after['AAPL'] > snap_state[0]


def test_snapshot_requests_are_serialized(snap_state, monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = {'n': 0}

    def daily(codes):
        calls['n'] += 1
        if calls['n'] == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
        return {code: [{'c': 100.0}] for code in codes}, set()

    monkeypatch.setattr(H, '_fetch_daily_bars', daily)
    monkeypatch.setattr(H, '_fetch_latest_quotes', lambda codes: (
        {code: _quote_at(snap_state, 101.0) for code in codes}, set()))
    results = []
    first = threading.Thread(target=lambda: results.append(
        H.latest_snapshot_batch(['AAPL'], force=True)), daemon=True)
    second = threading.Thread(target=lambda: results.append(
        H.latest_snapshot_batch(['AAPL'], force=True)), daemon=True)
    first.start()
    assert first_started.wait(2)
    second.start()
    assert second_started.wait(0.05) is False
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert second_started.is_set()
    assert H.latest_snapshot_batch(['AAPL'])['AAPL']['price'] == 101.0


def test_stream_propagates_rest_stale_marker(monkeypatch):
    monkeypatch.setattr(S, '_quotes', {})
    monkeypatch.setattr(H, 'latest_snapshot_batch', lambda codes: {
        'AAPL': {'price': 101.0, 'yest_close': 100.0, 'change_pct': 1.0, 'stale': True},
    })
    assert S.get_quotes_batch(['AAPL'])['AAPL']['stale'] is True


def test_ws_freshness_uses_event_time_and_rejects_replay(monkeypatch):
    now = [datetime.datetime(2026, 10, 29, 14, 0, tzinfo=datetime.timezone.utc).timestamp()]
    monkeypatch.setattr(S, '_quotes', {})
    monkeypatch.setattr(S, 'time', SimpleNamespace(time=lambda: now[0]))
    event = datetime.datetime(2026, 10, 29, 13, 59, 50, tzinfo=datetime.timezone.utc)
    event_ts = event.timestamp()
    event_iso = event.isoformat()

    S._update_quote('EVENT', 10.0, event_iso)
    assert S._read_quote('EVENT')['ts'] == event_ts
    assert S._read_quote('EVENT')['quote_t'] == event_iso

    now[0] += 20
    S._update_quote('EVENT', 99.0, (event - datetime.timedelta(seconds=1)).isoformat())
    assert S._read_quote('EVENT')['price'] == 10.0
    assert S._read_quote('EVENT')['ts'] == event_ts

    now[0] = event_ts + S.STREAM_STALE
    monkeypatch.setattr(H, 'latest_snapshot_batch', lambda codes: {})
    assert S.get_quotes_batch(['EVENT']) == {}    # 本地刚收到也不能延长事件新鲜期


def test_ws_quote_without_valid_event_time_is_not_cached(monkeypatch):
    monkeypatch.setattr(S, '_quotes', {})
    S._update_quote('EVENT', 10.0, None)
    S._update_quote('EVENT', 10.0, 'bad')
    assert S._read_quote('EVENT') is None
