#!/usr/bin/env python3
"""
alpaca_stream.py — Alpaca 美股实时行情（WebSocket，免费版 IEX 口径）

分工约定（整个美股方案统一遵循「实时=WS、历史=REST」）：
  - 实时推送：本文件后台线程连 wss://stream.data.alpaca.markets/v2/iex，
    订阅监控股票的 trades（成交）+ quotes（报价，覆盖盘前盘后），维护内存缓存
  - 历史/兜底：调 alpaca_history.py（REST），凭证也统一由它保管

运行策略：
  - 仅在 Alpaca 数据窗口（ET 周一至五 04:00-20:00，含盘前盘后）保持连接，
    其余时段断线省资源 —— 此时行情由 REST 兜底显示最近收盘价
  - 断线自动重连：5s 起步指数退避，封顶 60s
  - 读缓存（get_quotes_batch）永远不阻塞：stream 有数据用 stream，
    没有的代码回退 REST（REST 自带 60s TTL 节流，休市不浪费配额）
  - 连接代理：走本地 127.0.0.1:7890（直连访问不到 Alpaca，与 REST 口径一致）
"""

import datetime
import json
import threading
import time

import websocket  # websocket-client 库

import alpaca_history

STREAM_URL = 'wss://stream.data.alpaca.markets/v2/iex'   # 免费版 IEX 流（路径必须小写，大写 /v2/IEX 实测 404）
STREAM_STALE = 300          # stream 缓存超过 5 分钟视为过期，改走 REST
RETRY_BASE = 5              # 重连退避起步秒数
RETRY_MAX = 60              # 重连退避封顶秒数
POLL_INTERVAL = 15          # 非数据窗口下的轮询间隔（秒）
CONNECT_TIMEOUT = 5         # TCP/代理/TLS/WebSocket 握手各阶段的 socket 超时（秒）
CLOSE_TIMEOUT = 3           # 主动关闭时等待 close frame 的上限（秒）
CLOCK_SKEW = 5              # 行情事件时间允许略超前本地时钟的秒数


# ============================================================
# 模块级实时缓存（工作线程写、行情线程读，加锁保护）
# ============================================================
_quotes_lock = threading.Lock()
_quotes = {}                # {symbol: {'price','ts','quote_t'}}，ts 为行情事件时间

# 昨收补拉：REST 绝不能发生在 on_message 里 —— 收流线程被阻塞期间不读 socket，
# ping_timeout 会把它误判成断线，进而引发重连风暴（且 force 绕开 TTL 更费配额）。
# 这里改成「登记 → 独立线程慢慢拉」。
_pending_lock = threading.Lock()
_pending_prev = set()       # 待补昨收的 symbol
_pending_wake = threading.Event()
_prev_fetcher = None
PREV_FETCH_MIN_INTERVAL = 5   # 两次补拉的最小间隔（秒），沿用原节流口径防打满配额


def _request_prev_close(symbols):
    """登记需要补拉昨收的代码（不碰网络，可在 WS 回调里安全调用）"""
    global _prev_fetcher
    with _pending_lock:
        _pending_prev.update(s.upper() for s in symbols if s)
        if not _pending_prev:
            return
    if _prev_fetcher is None or not _prev_fetcher.is_alive():
        _prev_fetcher = threading.Thread(target=_prev_fetch_loop, daemon=True,
                                         name='alpaca-prev-fetch')
        _prev_fetcher.start()
    _pending_wake.set()


def _prev_fetch_loop():
    last_fetch = 0.0
    while True:                      # daemon 线程，随进程退出
        _pending_wake.wait()
        _pending_wake.clear()
        gap = PREV_FETCH_MIN_INTERVAL - (time.time() - last_fetch)
        if gap > 0:
            time.sleep(gap)
        with _pending_lock:
            batch = sorted(_pending_prev)
            _pending_prev.clear()
        if not batch:
            continue
        last_fetch = time.time()
        try:
            alpaca_history.latest_snapshot_batch(batch, force=True)
        except Exception as e:
            print(f'[Alpaca WS] 昨收补拉失败: {e}')


def _update_quote(symbol, price, quote_t=None):
    """按行情事件时间写入实时价；旧事件/补发不能刷新新鲜期。"""
    if price is None or price <= 0:
        return
    event_time = alpaca_history._parse_iso(quote_t)
    if event_time is None:
        return
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=datetime.timezone.utc)
    event_ts = event_time.timestamp()
    now = time.time()
    if event_ts > now + CLOCK_SKEW:
        return
    with _quotes_lock:
        entry = _quotes.setdefault(symbol, {'price': None, 'ts': 0.0, 'quote_t': None})
        if event_ts < entry['ts']:
            return
        entry['price'] = float(price)
        entry['ts'] = event_ts
        entry['quote_t'] = quote_t


def _read_quote(symbol):
    with _quotes_lock:
        entry = _quotes.get(symbol)
        return dict(entry) if entry else None


# ============================================================
# 对外取数：stream 缓存优先，REST 兜底
# ============================================================
def get_quotes_batch(codes):
    """美股统一取数入口（悬浮窗行情线程每轮调用，绝不阻塞）

    返回 {code: {'price','yest_close','change_pct'}}：
      - stream 缓存新鲜（5 分钟内，覆盖休市后保留的最后成交价）→ 用缓存价
      - 缺的代码 → alpaca_history.latest_snapshot_batch（REST，60s TTL 节流）
    涨跌幅基准（昨收）统一取 alpaca_history 的缓存，保证与 REST 口径一致。
    """
    codes = [c.upper() for c in codes if c]
    if not codes:
        return {}

    results = {}
    missing = []
    now = time.time()
    for code in codes:
        entry = _read_quote(code)
        if entry and entry.get('price') and now - entry['ts'] < STREAM_STALE:
            results[code] = _build_quote(code, entry['price'], entry['ts'])
        else:
            missing.append(code)

    if missing:
        # REST 结果自带 TTL 缓存；未配置凭证时返回 {}，卡片保持 '--'
        snaps = alpaca_history.latest_snapshot_batch(missing)
        for code in missing:
            snap = snaps.get(code)
            if snap:
                results[code] = {
                    'price': snap.get('price'),
                    'yest_close': snap.get('yest_close'),
                    'change_pct': snap.get('change_pct'),
                    'stale': bool(snap.get('stale')),
                    'quote_ts': snap.get('quote_ts'),
                }
    return results


def _build_quote(symbol, price, quote_ts=None):
    """stream 价 + 昨收 → 与 A 股一致的 quote 结构"""
    prev = alpaca_history.get_prev_close(symbol)
    change_pct = None
    if prev:
        change_pct = round((price - prev) / prev * 100, 2)
    return {'price': price, 'yest_close': prev, 'change_pct': change_pct,
            'quote_ts': quote_ts}


# ============================================================
# WebSocket 工作线程
# ============================================================
def _credential_snapshot():
    with alpaca_history._creds_lock:
        creds = alpaca_history._creds
        return creds['key'], creds['secret']


def _close_websocket(ws):
    if ws:
        try:
            ws.close(timeout=CLOSE_TIMEOUT)
        except Exception:
            pass


class _AlpacaStreamThread(threading.Thread):
    """后台线程：窗口判定 → 连接 → 认证 → 订阅 → 收消息 → 断线退避重连"""

    def __init__(self):
        super().__init__(daemon=True, name='alpaca-stream')
        self._stop_event = threading.Event()
        self._symbols = []
        self._symbols_lock = threading.Lock()
        self._ws = None
        self._authed = False          # 本次连接是否已完成认证（决定何时发订阅）
        self._session_error = False   # 认证后异常断线不能重置退避
        self._credentials = _credential_snapshot()
        self._generation = 0          # symbols/凭证变更时使正在握手的连接失效
        self.stopping = False         # 已被要求停止（生命周期管理据此判断能否复用）

    # ---------- 控制接口 ----------
    def set_symbols(self, symbols):
        """更新订阅列表；连接活跃时关掉当前连接触发重连（重连时用新列表）"""
        symbols = [s.upper() for s in symbols if s]
        with self._symbols_lock:
            changed = symbols != self._symbols
            self._symbols = list(symbols)
            if changed:
                self._generation += 1
        if changed:
            _close_websocket(self._ws)

    def credentials_changed(self):
        """凭证变化时废弃当前连接，即使订阅列表相同也重新认证。"""
        credentials = _credential_snapshot()
        with self._symbols_lock:
            if credentials == self._credentials:
                return
            self._credentials = credentials
            self._generation += 1
        _close_websocket(self._ws)

    def stop(self):
        self.stopping = True
        self._stop_event.set()
        _close_websocket(self._ws)

    def _get_symbols(self):
        with self._symbols_lock:
            return list(self._symbols)

    # ---------- 主循环 ----------
    def run(self):
        backoff = RETRY_BASE
        while not self._stop_event.is_set():
            symbols = self._get_symbols()
            # 无凭证/无订阅/不在数据窗口 → 休眠等待（此时行情走 REST 兜底）
            if not symbols or not alpaca_history.has_credentials() \
                    or not alpaca_history.in_us_data_window():
                self._stop_event.wait(POLL_INTERVAL)      # 可被 stop() 立刻打断
                continue

            authed = False
            try:
                authed = self._run_once(symbols)
            except Exception as e:
                print(f'[Alpaca WS] 连接异常: {e}')
            # run_forever 会把连接/认证失败吞成正常返回（websocket-client 实测），
            # 所以「有没有抛异常」不能当判据 —— 只有真正认证过的一轮才算恢复，
            # 否则退避会恒被重置成 5 秒，代理没开时通宵每 5 秒重试一次
            if authed:
                backoff = RETRY_BASE

            if self._stop_event.is_set():
                break
            # 断开后：仍在数据窗口内则退避重连，否则回主循环等窗口
            if alpaca_history.in_us_data_window() and self._get_symbols():
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, RETRY_MAX)

    def _run_once(self, symbols):
        """单次连接的完整生命周期（阻塞到断开）；返回本轮是否认证成功过"""
        self._authed = False
        self._session_error = False
        if self._stop_event.is_set():
            return False
        credentials = _credential_snapshot()
        previous_timeout = websocket.getdefaulttimeout()
        with self._symbols_lock:
            self._credentials = credentials
            generation = self._generation

        def _close_if_stopping(ws):
            if self._stop_event.is_set():
                _close_websocket(ws)

        def on_open(ws):
            # 握手期间 symbols/凭证又变过 → 不认证旧连接，交给下一轮重建。
            with self._symbols_lock:
                obsolete = generation != self._generation
            if self._stop_event.is_set() or obsolete:
                websocket.setdefaulttimeout(previous_timeout)
                _close_websocket(ws)
                return
            websocket.setdefaulttimeout(previous_timeout)   # 握手已完成，恢复进程默认值
            if getattr(ws, 'sock', None) is not None:
                ws.sock.settimeout(previous_timeout)       # 已连接的 socket 也不能沿用握手超时
            # 认证帧：凭证只进网络包，不打印日志
            with alpaca_history._creds_lock:
                key, secret = alpaca_history._creds['key'], alpaca_history._creds['secret']
            ws.send(json.dumps({'action': 'auth', 'key': key, 'secret': secret}))

        def on_message(ws, message):
            _close_if_stopping(ws)
            try:
                payload = json.loads(message)
            except ValueError:
                return
            if not isinstance(payload, list):
                payload = [payload]
            for item in payload:
                self._handle_item(item)

        def on_error(ws, error):
            # 非正常关闭的错误都会走到这里，主循环负责退避重连
            self._session_error = True
            print(f'[Alpaca WS] 错误: {error}')

        def on_close(ws, close_status, close_msg):
            # 正常断线仍保留「本轮认证过」供 run() 重置退避；停止或异常码不重置。
            if self._stop_event.is_set() or close_status not in (1000, 1001):
                self._authed = False

        # run_forever 没有握手 timeout 参数；其 connect 路径使用进程默认 socket
        # timeout。生命周期保证只有一条 Alpaca 流，故仅在握手窗口覆盖该默认值。
        websocket.setdefaulttimeout(CONNECT_TIMEOUT)
        try:
            if self._stop_event.is_set():
                return False
            self._ws = websocket.WebSocketApp(
                STREAM_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            if generation != self._generation or credentials != _credential_snapshot():
                return False
            self._ws.run_forever(
                ping_interval=20, ping_timeout=10,
                http_proxy_host=alpaca_history.PROXY_HOST,
                http_proxy_port=alpaca_history.PROXY_PORT,
                http_proxy_timeout=CONNECT_TIMEOUT,
                proxy_type='http',
            )
        finally:
            websocket.setdefaulttimeout(previous_timeout)
            authed = self._authed and not self._session_error
            ws = self._ws
            self._ws = None
            self._authed = False
            self._session_error = False
            if self._stop_event.is_set():
                authed = False
            _close_websocket(ws)
        return authed

    # ---------- 消息处理 ----------
    def _handle_item(self, item):
        if self._stop_event.is_set():
            return
        t = item.get('T')
        if t == 'success':
            msg = item.get('msg', '')
            if msg == 'authenticated':
                self._authed = True
                self._send_subscribe()
            return
        if t == 'subscription':
            return                                   # 订阅确认，无需处理
        if t == 'error':
            code = item.get('code')
            msg = item.get('msg')
            if code == 40110000:
                # 认证失败：连接会被服务端断开，这里给明确提示便于排查（不打印凭证）
                print('[Alpaca WS] 认证失败(40110000)：请在设置中检查 API Key/Secret')
            else:
                print(f'[Alpaca WS] 服务端错误 {code}: {msg}')
            return
        if t == 'hb':
            return                                   # 服务端心跳
        if t == 't':                                  # 成交（trade）
            price = item.get('p')
            self._update_prev_then_quote(item.get('sym'), price, item.get('t'))
            return
        if t == 'q':                                  # 报价（quote，覆盖盘前盘后）
            sym = item.get('sym')
            # 与 REST 同一口径：成交价优先、宽价差直接丢弃这条报价。
            # 盘前挂单噪声大，中值当现价会显示假涨跌并可能误触目标价预警
            ref = alpaca_history.get_prev_close(sym) if sym else None
            price = alpaca_history.pick_quote_price(
                item.get('bp'), item.get('ap'), item.get('p'), ref)
            if price is not None:
                self._update_prev_then_quote(sym, price, item.get('t'))
            return

    def _update_prev_then_quote(self, sym, price, quote_t):
        if not sym or price is None:
            return
        # 昨收还没拿到（首次连接/跨日）→ 登记给后台线程补拉。
        # 节流由补拉线程统一做（原来一个全局时间戳会让后缺基准的票一直等不到补拉）
        if alpaca_history.get_prev_close(sym) is None:
            _request_prev_close([sym])
        _update_quote(sym.upper(), price, quote_t)

    def _send_subscribe(self):
        symbols = self._get_symbols()
        if not symbols:
            return
        # trades=最新成交价；quotes=盘前盘后连续报价（IEX 上报价更新比成交更密）
        try:
            self._ws.send(json.dumps({
                'action': 'subscribe',
                'trades': symbols,
                'quotes': symbols,
            }))
            print(f'[Alpaca WS] 已订阅 {len(symbols)} 只美股实时行情')
            # 订阅成功后补一次 REST 快照：拿到昨收，涨跌幅立即可算（同样不在收流线程里做）
            _request_prev_close(symbols)
        except Exception as e:
            print(f'[Alpaca WS] 订阅失败: {e}')


# ============================================================
# 生命周期管理（由 floating_window 调用）
# ============================================================
_thread = None
_thread_lock = threading.RLock()   # handoff 持有锁完成最新列表启动，避免旧 symbols 回写
_pending_symbols = []       # 最近一次要求的订阅列表（交接手线程用最新的）


def ensure_started(symbols):
    """确保 stream 线程运行（启动/配置变更时调用）；symbols 为空则停掉"""
    global _thread, _pending_symbols
    symbols = [s.upper() for s in symbols if s]
    with _thread_lock:
        _pending_symbols = list(symbols)
        if not symbols or not alpaca_history.has_credentials():
            _stop_locked()
            return
        if _thread is not None and _thread.is_alive():
            if not _thread.stopping:
                _thread.set_symbols(symbols)
            return          # 正在收尾的线程由 handoff 用最新列表接手，这里不叠第二条连接
        _thread = _AlpacaStreamThread()
        _thread.set_symbols(symbols)
        _thread.start()


def credentials_changed():
    """alpaca_history.configure 后调用；订阅不变也会强制重新认证。"""
    with _thread_lock:
        if _thread is not None and _thread.is_alive() and not _thread.stopping:
            _thread.credentials_changed()


def stop():
    global _pending_symbols
    with _thread_lock:
        _pending_symbols = []       # 交接手线程据此不再重启
        _stop_locked()


def _stop_locked():
    """停掉当前流线程。

    join 超时说明它还卡在握手/收流里 —— 此时不能像原先那样把引用置 None：
    下一次 ensure_started 会再起一条，形成双连接双订阅（配额翻倍、两个线程交替写同一缓存）。
    改为交给一个一次性线程：等旧线程真退出后，再用最新列表接手启动。
    """
    global _thread
    if _thread is None:
        return
    old = _thread
    old.stop()
    old.join(timeout=3)
    if not old.is_alive():
        # 注意：threading.Thread.join() 的返回值在本机 Python 是 None（不是布尔），
        # 判断线程有没有真退出只能看 is_alive()
        _thread = None
        return
    print('[Alpaca WS] 旧流线程 3 秒内未退出，等它结束后再接手（不叠第二条连接）')
    threading.Thread(target=_handoff, args=(old,), daemon=True,
                     name='alpaca-stream-handoff').start()


def _handoff(old):
    """持续等待旧线程退出，再用最新 _pending_symbols 接手；超时不放弃恢复。"""
    global _thread
    while old.is_alive():
        old.join(timeout=CONNECT_TIMEOUT + CLOSE_TIMEOUT + 2)
        if old.is_alive():
            with _thread_lock:
                if _thread is not old:
                    return
            print('[Alpaca WS] 旧流线程仍未退出，继续等待后接手（不叠第二条连接）')
    with _thread_lock:
        if _thread is not old:
            return                  # 期间已被别的调用重建
        _thread = None
        # RLock 内完成读取与启动，ensure_started 的更新要么在前、要么在后，
        # 交接线程不能用释放锁前读到的旧 symbols 覆盖新列表。
        ensure_started(_pending_symbols)


def is_running():
    return _thread is not None and _thread.is_alive() and not _thread.stopping
