"""P1 回归：WS 退避/停启生命周期/昨收补拉不阻塞收流"""
import datetime
import json
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
        self.credentials_calls = 0

    def credentials_changed(self):
        self.credentials_calls += 1

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
    handoffs = []

    class _NoRun:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            handoffs.append(self)

    clean.setattr(S.threading, 'Thread', _NoRun)
    S.stop()

    assert lingering.stop_calls == 1
    assert S._thread is lingering                       # 仍被占住，不叠第二条
    assert S.is_running() is False                      # 但对外不再算「在运行」
    assert len(handoffs) == 1 and handoffs[0].target is S._handoff
    assert handoffs[0].args == (lingering,)


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
    th._update_prev_then_quote(
        'AAPL', 10.0, datetime.datetime.now(datetime.timezone.utc).isoformat())

    assert calls == []                                  # 当场没有任何 REST
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.05)
    assert calls == [['AAPL']]                          # 由后台线程补拉
    assert S._read_quote('AAPL')['price'] == 10.0       # 实时价照常写入


class _FakeApp:
    def __init__(self, url, socket=None, **callbacks):
        self.url = url
        self.socket = socket
        self.__dict__.update(callbacks)
        self.close_calls = []
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def close(self, **kwargs):
        self.close_calls.append(kwargs)


def _install_fake_ws(monkeypatch, behavior):
    monkeypatch.setattr(S, '_request_prev_close', lambda symbols: None)
    apps = []

    class App(_FakeApp):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            apps.append(self)

        def run_forever(self, **kwargs):
            behavior(self, kwargs)

    monkeypatch.setattr(S.websocket, 'WebSocketApp', App)
    return apps


def test_authenticated_session_still_counts_after_normal_on_close(clean):
    th = S._AlpacaStreamThread()
    th.set_symbols(['AAPL'])
    previous = S.websocket.getdefaulttimeout()
    socket_timeouts = []

    class Socket:
        def settimeout(self, timeout):
            socket_timeouts.append(timeout)

    def behavior(app, kwargs):
        app.sock = Socket()
        assert S.websocket.getdefaulttimeout() == S.CONNECT_TIMEOUT
        app.on_open(app)
        assert S.websocket.getdefaulttimeout() == previous
        assert socket_timeouts == [previous]
        app.on_message(app, '[{"T":"success","msg":"authenticated"}]')
        app.on_close(app, 1000, 'closed')

    _install_fake_ws(clean, behavior)
    assert th._run_once(['AAPL']) is True
    assert th._authed is False                          #  finally 仍清理本轮状态


def test_abnormal_disconnect_does_not_reset_backoff(clean):
    th = S._AlpacaStreamThread()
    th.set_symbols(['AAPL'])

    def behavior(app, kwargs):
        app.on_open(app)
        app.on_message(app, '[{"T":"success","msg":"authenticated"}]')
        app.on_error(app, TimeoutError('ping timeout'))
        app.on_close(app, 1006, 'abnormal')

    _install_fake_ws(clean, behavior)
    assert th._run_once(['AAPL']) is False


def test_stop_during_session_clears_authenticated_state(clean):
    th = S._AlpacaStreamThread()
    th.set_symbols(['AAPL'])

    def behavior(app, kwargs):
        app.on_open(app)
        app.on_message(app, '[{"T":"success","msg":"authenticated"}]')
        th.stop()
        app.on_close(app, 1000, 'stopped')

    apps = _install_fake_ws(clean, behavior)
    assert th._run_once(['AAPL']) is False
    assert th._authed is False
    assert apps[0].close_calls


def test_handshake_has_explicit_timeout_and_restores_default(clean):
    th = S._AlpacaStreamThread()
    previous = S.websocket.getdefaulttimeout()

    class App(_FakeApp):
        def run_forever(self, **kwargs):
            assert S.websocket.getdefaulttimeout() == S.CONNECT_TIMEOUT
            assert kwargs['http_proxy_timeout'] == S.CONNECT_TIMEOUT
            raise TimeoutError('offline handshake timeout')

    clean.setattr(S.websocket, 'WebSocketApp', App)
    with pytest.raises(TimeoutError, match='offline handshake timeout'):
        th._run_once(['AAPL'])
    assert S.websocket.getdefaulttimeout() == previous


def test_real_websocket_app_receives_handshake_timeout(clean):
    """走真实 WebSocketApp.run_forever，只替换底层 socket，验证超时确实进入连接路径。"""
    th = S._AlpacaStreamThread()
    previous = S.websocket.getdefaulttimeout()
    timeouts = []
    connect_options = []

    class OfflineSocket:
        def __init__(self, *args, **kwargs):
            pass

        def settimeout(self, timeout):
            timeouts.append(timeout)

        def connect(self, url, **kwargs):
            connect_options.append(kwargs)
            raise TimeoutError('offline socket timeout')

        def close(self):
            pass

    clean.setattr(S.websocket._app, 'WebSocket', OfflineSocket)
    assert th._run_once(['AAPL']) is False
    assert timeouts and timeouts[0] == S.CONNECT_TIMEOUT
    assert connect_options[0]['http_proxy_timeout'] == S.CONNECT_TIMEOUT
    assert S.websocket.getdefaulttimeout() == previous


def test_stop_during_handshake_does_not_enter_message_loop(clean):
    th = S._AlpacaStreamThread()
    apps = []

    class App(_FakeApp):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            apps.append(self)

        def run_forever(self, **kwargs):
            assert S.websocket.getdefaulttimeout() == S.CONNECT_TIMEOUT
            th.stop()                                    # 模拟握手被另一线程要求停止
            self.on_close(self, 1000, 'stopped')         # 不触发 on_open/收流

    clean.setattr(S.websocket, 'WebSocketApp', App)
    assert th._run_once(['AAPL']) is False
    assert apps[0].sent == []
    assert apps[0].close_calls


def test_credentials_change_rebuilds_connection_with_same_symbols(clean):
    th = S._AlpacaStreamThread()
    th.set_symbols(['AAPL'])
    app = _FakeApp(S.STREAM_URL)
    th._ws = app

    th.credentials_changed()                           # 凭证未变，不重连
    assert app.close_calls == []
    H.configure('new-key', 'new-secret')
    th.credentials_changed()
    assert app.close_calls == [{'timeout': S.CLOSE_TIMEOUT}]
    assert th._get_symbols() == ['AAPL']


def test_changed_credentials_are_used_by_replacement_connection(clean):
    th = S._AlpacaStreamThread()
    th.set_symbols(['AAPL'])
    auth_keys = []
    runs = [0]

    def behavior(app, kwargs):
        app.on_open(app)
        auth_keys.append(json.loads(app.sent[-1])['key'])
        app.on_message(app, '[{"T":"success","msg":"authenticated"}]')
        runs[0] += 1
        if runs[0] == 1:
            H.configure('new-key', 'new-secret')
            th.credentials_changed()
        app.on_close(app, 1000, 'reconnect')

    apps = _install_fake_ws(clean, behavior)
    assert th._run_once(['AAPL']) is True
    assert th._run_once(['AAPL']) is True
    assert auth_keys == ['k', 'new-key']
    assert apps[0].close_calls
    assert th._get_symbols() == ['AAPL']


def test_public_credentials_changed_notifies_live_thread(clean):
    th = _FakeThread(alive=True)
    clean.setattr(S, '_thread', th)
    S.credentials_changed()
    assert th.credentials_calls == 1


def test_handoff_retries_after_timeout_then_starts_latest_symbols(clean):
    class _EventuallyExits:
        stopping = True
        joins = 0

        def is_alive(self):
            return self.joins < 2

        def join(self, timeout=None):
            self.joins += 1

    old = _EventuallyExits()
    clean.setattr(S, '_thread', old)
    clean.setattr(S, '_pending_symbols', ['MSFT'])
    created = _start_capture(clean)
    S._handoff(old)
    assert old.joins == 2                               # 超时后继续等，没有永久放弃
    assert len(created) == 1
    assert created[0]._get_symbols() == ['MSFT']


def test_handoff_start_is_atomic_with_newer_ensure_started(clean):
    old = _FakeThread(alive=False)
    old.stopping = True
    clean.setattr(S, '_thread', old)
    clean.setattr(S, '_pending_symbols', ['AAPL'])
    created = _start_capture(clean)
    real_ensure = S.ensure_started
    entered = threading.Event()
    release = threading.Event()
    updater_done = threading.Event()
    first = [True]

    def intercepted_ensure(symbols):
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(2)
        return real_ensure(symbols)

    clean.setattr(S, 'ensure_started', intercepted_ensure)
    handoff = threading.Thread(target=S._handoff, args=(old,))
    handoff.start()
    assert entered.wait(2)

    def update():
        real_ensure(['MSFT'])
        updater_done.set()

    updater = threading.Thread(target=update)
    updater.start()
    try:
        # 新实现持锁启动，更新线程必须等交接完成；旧实现会在这里完成并覆盖列表。
        assert not updater_done.wait(0.1)
    finally:
        release.set()
        handoff.join(2)
        updater.join(2)
    assert not handoff.is_alive() and not updater.is_alive()
    assert S._pending_symbols == ['MSFT']
    assert S._thread._get_symbols() == ['MSFT']
    assert len(created) == 2
