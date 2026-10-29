"""P1 回归：WS 退避/停启生命周期/昨收补拉不阻塞收流"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import pytest

import alpaca_history as H
import alpaca_stream as S


class _FakeThread:
    """假流线程：只覆盖生命周期管理用到的那几个成员。
    注意 join() 的返回值不可信（本机 Python 恒返回 None），产品代码判的是 is_alive()"""

    def __init__(self, alive=False):
        self._alive = alive
        self.stopping = False
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        self.stopping = True

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(S, '_thread', None)
    monkeypatch.setattr(S, '_pending_symbols', [], raising=False)
    monkeypatch.setattr(H, '_creds', {'key': 'k', 'secret': 's'}, raising=True)
    return monkeypatch


def _start_capture(monkeypatch):
    """拦住真起线程的动作，只记录被创建了几次"""
    created = []
    real_cls = S._AlpacaStreamThread

    class _NoStart(real_cls):
        def start(self_inner):
            created.append(self_inner)

    monkeypatch.setattr(S, '_AlpacaStreamThread', _NoStart)
    return created


def test_backoff_actually_grows_when_connection_never_authenticates(clean):
    """代理没开/凭证错时 run_forever 正常返回不抛异常（实测），
    旧实现据此把退避恒重置成 5 秒 —— 通宵每 5 秒重试一次"""
    th = S._AlpacaStreamThread()
    waits = []
    th._stop_event.wait = lambda timeout=None: waits.append(timeout)
    th._get_symbols = lambda: ['AAPL']
    clean.setattr(H, 'has_credentials', lambda: True)
    clean.setattr(H, 'in_us_data_window', lambda *a: True)

    calls = []

    def fake_run_once(symbols):
        calls.append(1)
        if len(calls) >= 4:
            th._stop_event.set()
        return False                      # 从未认证成功

    th._run_once = fake_run_once
    th.run()

    assert waits == [5, 10, 20]           # 指数退避真的在长


def test_backoff_resets_after_a_good_session(clean):
    th = S._AlpacaStreamThread()
    waits = []
    th._stop_event.wait = lambda timeout=None: waits.append(timeout)
    th._get_symbols = lambda: ['AAPL']
    clean.setattr(H, 'has_credentials', lambda: True)
    clean.setattr(H, 'in_us_data_window', lambda *a: True)

    calls = []

    def fake_run_once(symbols):
        calls.append(1)
        if len(calls) >= 3:
            th._stop_event.set()
        return True                       # 连上过

    th._run_once = fake_run_once
    th.run()

    assert waits == [5, 5]


def test_clean_stop_clears_the_reference(clean):
    """线程 3 秒内真退出了：引用必须清空，下次 ensure_started 才能干净重建"""
    done = _FakeThread(alive=False)
    clean.setattr(S, '_thread', done)

    S.stop()

    assert done.stop_calls == 1
    assert S._thread is None


def test_stop_keeps_reference_when_thread_lingers(clean):
    """join 超时不能把引用置空：否则下一次 ensure_started 会叠出第二条连接"""
    lingering = _FakeThread(alive=True)
    clean.setattr(S, '_thread', lingering)

    S.stop()

    assert lingering.stop_calls == 1
    assert S._thread is lingering                       # 仍被占住，不叠第二条
    assert S.is_running() is False                      # 但对外不再算「在运行」


def test_ensure_started_does_not_stack_on_a_stopping_thread(clean):
    lingering = _FakeThread(alive=True)
    lingering.stopping = True
    clean.setattr(S, '_thread', lingering)
    created = _start_capture(clean)

    S.ensure_started(['AAPL'])

    assert created == []
    assert S._thread is lingering


def test_prev_close_refetch_is_deferred_not_in_reader(clean):
    """on_message 里做阻塞 REST 会卡住收流 → pong 超时被误判断线 → 重连风暴"""
    calls = []
    clean.setattr(H, 'latest_snapshot_batch', lambda codes, force=False: calls.append(list(codes)))
    th = S._AlpacaStreamThread()
    th._update_prev_then_quote('AAPL', 10.0, 'ts')     # 昨收缺失 → 只登记，不打网络

    assert calls == []                                  # 当场没有任何 REST
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    assert calls == [['AAPL']]                          # 由后台线程补拉
    assert S._read_quote('AAPL')['price'] == 10.0       # 实时价照常写入
