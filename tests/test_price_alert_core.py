import os
import sys
import json
import threading

import pytest

# 注入 scripts/ 到 sys.path，使 import price_alert 可用（不依赖 Qt / eltdx）
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import price_alert  # noqa: E402


# ------------------------------------------------------------------
# P0 回归：TDX 锁必须可重入——get_quote_tdx 持锁 → get_tdx_client →
# close_tdx_client 会二次进入同一把锁，普通 Lock 会自死锁卡死取数线程。
# ------------------------------------------------------------------
def test_tdx_lock_is_reentrant():
    assert isinstance(price_alert._tdx_lock, type(threading.RLock())), \
        "_tdx_lock 必须是 RLock，否则 close_tdx_client 在持锁路径下自死锁"


def test_close_tdx_client_under_held_lock_does_not_deadlock():
    done = threading.Event()

    def body():
        # 模拟 get_quote_tdx 已持锁后再调用 close_tdx_client 的重入路径
        with price_alert._tdx_lock:
            price_alert.close_tdx_client()
        done.set()

    t = threading.Thread(target=body, daemon=True)
    t.start()
    assert done.wait(2.0), "close_tdx_client 在持有 _tdx_lock 时调用发生自死锁"


# ------------------------------------------------------------------
# 源失败屏蔽 / 恢复：验证 M3 的「计数达阈值屏蔽、成功即时解封」语义
# （纯内存操作，不触网）
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_source_state():
    price_alert._source_recover('tencent')
    yield
    price_alert._source_recover('tencent')


def test_source_disabled_after_max_fails_and_recovers_on_success():
    for _ in range(price_alert.MAX_CONSECUTIVE_FAILS):
        price_alert._source_mark_fail('tencent')
    assert price_alert._source_available('tencent') is False
    price_alert._source_mark_success('tencent')
    assert price_alert._source_available('tencent') is True


def test_source_not_disabled_below_threshold():
    for _ in range(price_alert.MAX_CONSECUTIVE_FAILS - 1):
        price_alert._source_mark_fail('tencent')
    assert price_alert._source_available('tencent') is True


# ------------------------------------------------------------------
# _batch_has_valid_result：至少覆盖一半且 price>0、change_pct 齐备。
# 固化正确语义（驳回「量极大时下界为1会放过坏源」的误报——下界仅对小列表生效）
# ------------------------------------------------------------------
def test_batch_valid_result_requires_half_coverage():
    codes = ['a', 'b', 'c', 'd']
    good = {'price': 1.0, 'change_pct': 0.5}
    assert price_alert._batch_has_valid_result({}, codes) is False
    assert price_alert._batch_has_valid_result({c: good for c in codes}, codes) is True
    # 4 只仅 1 条有效 < len//2=2 → 判不可用
    assert price_alert._batch_has_valid_result({'a': good}, codes) is False
    # 2/4 达半 → 可用
    assert price_alert._batch_has_valid_result({'a': good, 'b': good}, codes) is True


def test_batch_usable_requires_change_pct():
    codes = ['a', 'b']
    # 缺 change_pct 不算有效（涨跌幅预警依赖它）
    assert price_alert._batch_has_valid_result({'a': {'price': 1.0}}, codes) is False
    assert price_alert._batch_has_valid_result({'a': {'price': 0}}, codes) is False


def test_quote_timestamp_and_explicit_stale_marker(monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(price_alert.time, 'time', lambda: now)
    assert price_alert.quote_is_stale({'quote_ts': now - 10}) is False
    assert price_alert.quote_is_stale({'quote_ts': now - 91}) is True
    assert price_alert.quote_is_stale({'quote_ts': now + 61}) is True
    assert price_alert.quote_is_stale({'stale': True}) is True
    assert price_alert.quote_is_stale({'price': 10.0}) is False


def test_powershell_fallback_does_not_interpolate_message(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(
        ctypes, 'windll',
        SimpleNamespace(user32=SimpleNamespace(
            MessageBoxW=lambda *args: (_ for _ in ()).throw(OSError('offline')))),
        raising=False)
    monkeypatch.setattr(
        price_alert.subprocess, 'Popen',
        lambda args, **kwargs: calls.append((args, kwargs)))

    payload = 'x"; Start-Process calc; "'
    price_alert.show_popup('title', payload)

    args, kwargs = calls[0]
    assert payload not in args[-1]
    assert '$env:STOCK_MONITOR_MESSAGE' in args[-1]
    assert kwargs['env']['STOCK_MONITOR_MESSAGE'] == payload


def test_check_multi_rearms_after_condition_clears(monkeypatch, tmp_path):
    class StopLoop(Exception):
        pass

    config = tmp_path / 'alerts.json'
    log = tmp_path / 'alerts.log'
    config.write_text(json.dumps({
        'interval': 1, 'log': str(log), 'alerts': [
            {'code': 'sh600000', 'name': '测试', 'target': 10.0, 'dir': 'below'},
        ],
    }), encoding='utf-8')
    prices = iter([9.0, 11.0, 9.0])
    sleeps = {'count': 0}

    def fake_sleep(_seconds):
        sleeps['count'] += 1
        if sleeps['count'] >= 3:
            raise StopLoop

    monkeypatch.setattr(price_alert, 'get_quote', lambda *a, **k: {
        'name': '测试', 'price': next(prices), 'change_pct': 0.0})
    monkeypatch.setattr(price_alert, 'is_a_share_trading_time', lambda *a: True)
    monkeypatch.setattr(price_alert, 'quote_is_stale', lambda *a, **k: False)
    monkeypatch.setattr(price_alert.time, 'sleep', fake_sleep)
    try:
        price_alert.check_multi(str(config), source='tencent', notify='log')
    except StopLoop:
        pass

    alerts = [line for line in log.read_text(encoding='utf-8').splitlines()
              if '*** ALERT ***' in line]
    assert len(alerts) == 2
