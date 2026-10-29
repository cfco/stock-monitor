#!/usr/bin/env python3
"""
alpaca_history.py — Alpaca 美股历史数据（REST API，免费版 IEX 口径）

分工约定（整个美股方案统一遵循「实时=WS、历史=REST」）：
  - 本文件只管 REST：历史 K 线、最新价快照（休市兜底）、昨收缓存、交易时段判断
  - 实时推送在 alpaca_stream.py（WebSocket），它会反过来调用本文件

免费版（Basic plan）关键约束，代码据此设计：
  - 实时/历史数据口径统一用 feed=iex（免费版仅 IEX 交易所数据免费且实时，
    SIP 全市场实时需付费；请求带 feed=sip 会 403）
  - REST 限流 200 次/分钟，本模块自带本地限流器（上限 180，留余量）
  - 代理：美股数据统一走本地代理 127.0.0.1:7890（直连访问不到 Alpaca，用户指定）
  - 盘前盘后：latest quote / bars 在 ET 04:00-20:00 有数据，本模块统一覆盖

返回格式与 A 股接口对齐（复用悬浮图渲染）：
  K 线 = [{'time','open','high','low','close','volume'}, ...] 升序
    1min   → time='HH:MM'（北京时间）
    daily  → time='MM-DD'（北京时间日期）
  快照 = {code: {'price','yest_close','change_pct'}}
"""

import datetime
import json
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from zoneinfo import ZoneInfo

# ============================================================
# 常量与时区
# ============================================================
DATA_API = 'https://data.alpaca.markets/v2'          # 行情数据 API（K线/报价/快照）
TRADING_API = 'https://paper-api.alpaca.markets/v2'  # 交易 API（免费版默认 paper 账户，assets 名称在这）
LIVE_API = 'https://api.alpaca.markets/v2'           # 交易 API 实盘地址（paper key 打这里会 401，兜底用）
FEED = 'iex'                    # 免费版唯一可用的实时口径
MAX_REQ_PER_MIN = 180           # 官方 200/分钟，留 20 次余量避免 429
SNAPSHOT_TTL = 60               # 快照缓存有效期（秒）：休市时价格不变，不必每轮请求
SYMBOLS_PER_BATCH = 250         # Alpaca 批量 symbols 单次上限
# 盘前盘后用买卖中值当现价的前提：价差够窄。实测（ET 05:28 盘前）AAPL
# bp=325.91 / ap=360.46（价差 10%）且无成交，中值 343.19 比昨收低 1.1%，
# 直接取中值会显示假涨跌、并可能误触目标价预警 —— 价差超这个比例就不认这条报价
MAX_SPREAD_RATIO = 0.01

TZ_ET = ZoneInfo('America/New_York')   # 美东（判断交易时段/是否"今天"）
TZ_SH = ZoneInfo('Asia/Shanghai')      # 北京（K 线时间展示口径）

# 周期映射：悬浮图周期 → Alpaca timeframe
TIMEFRAME_MAP = {'1min': '1Min', 'daily': '1Day', 'weekly': '1Week'}

# 美股数据必须走本地代理（直连访问不到 Alpaca，用户指定 127.0.0.1:7890）
# 显式传 dict 覆盖：既不直连，也不会误用环境变量里的其它代理
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 7890
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({
    'http': f'http://{PROXY_HOST}:{PROXY_PORT}',
    'https': f'http://{PROXY_HOST}:{PROXY_PORT}',
}))

# ============================================================
# 凭证（由 floating_window 启动时从配置注入）
# ============================================================
_creds = {'key': '', 'secret': ''}
_creds_lock = threading.Lock()


def configure(api_key, secret):
    """注入 Alpaca API 凭证（设置界面保存后调用）"""
    with _creds_lock:
        _creds['key'] = (api_key or '').strip()
        _creds['secret'] = (secret or '').strip()


def has_credentials():
    """是否已配置可用凭证（未配置时美股功能整体静默停用）"""
    with _creds_lock:
        return bool(_creds['key'] and _creds['secret'])


def _headers():
    """REST 请求头：凭证放在 Header，绝不打印到日志"""
    with _creds_lock:
        key, secret = _creds['key'], _creds['secret']
    return {
        'APCA-API-KEY-ID': key,
        'APCA-API-SECRET-KEY': secret,
        'Accept': 'application/json',
        'User-Agent': 'stock-monitor/1.0',
    }


# ============================================================
# 交易时段判断（含盘前盘后，选项5：包含 extended hours）
# ============================================================
def in_us_data_window(now=None):
    """是否处于 Alpaca 有数据的时段：ET 周一至五 04:00-20:00

    ET 04:00-09:30 盘前、09:30-16:00 正式盘、16:00-20:00 盘后。
    北京时间对应约 17:00/18:00 - 次日 08:00/09:00（覆盖夜盘关注时段）。
    节假日 Alpaca 流基本无数据，连接本身无害，不单独判节假日。
    """
    now = now or datetime.datetime.now(TZ_ET)
    if now.weekday() >= 5:                      # 周六/周日无数据
        return False
    minutes = now.hour * 60 + now.minute
    return 4 * 60 <= minutes < 20 * 60


# ============================================================
# 本地限流器（免费版 200 请求/分钟）
# ============================================================
_rate_lock = threading.Lock()
_req_times = []          # 最近 60 秒内的请求时间戳
_cool_until = 0.0        # 收到 429 后的冷却截止时间


def _allow_request():
    """本地节流：60 秒窗口内不超过 MAX_REQ_PER_MIN 次；429 冷却期内直接拒绝"""
    global _cool_until
    now = time.time()
    with _rate_lock:
        if now < _cool_until:
            return False
        # 清出窗口外的旧记录
        while _req_times and now - _req_times[0] > 60:
            _req_times.pop(0)
        if len(_req_times) >= MAX_REQ_PER_MIN:
            return False
        _req_times.append(now)
        return True


def _mark_rate_limited():
    """收到 429：冷却 60 秒，期间所有 REST 请求直接跳过，避免雪上加霜"""
    global _cool_until
    with _rate_lock:
        _cool_until = time.time() + 60
    print('[Alpaca REST] 触发限流(429)，本地冷却 60 秒')


# ============================================================
# HTTP 底层
# ============================================================
def _http_get(path, params=None, timeout=6, base=None, quiet_401=False):
    """GET Alpaca REST，返回 dict；失败/限流/无数据返回 None

    base: 指定 API 地址（默认行情 DATA_API；assets 等交易 API 端点传 TRADING_API/LIVE_API）
    quiet_401: 401 不打印 —— paper key 打 live 地址（或反之）会 401，
               这是「地址不适用」而非凭证错误，打印会误导排查
    注意：404（该 symbol 无数据）与 401（凭证无效）都归入 None，
    由调用方按空结果降级处理，不让单只股票拖垮整批。
    """
    if not has_credentials():
        return None
    if not _allow_request():
        return None

    url = (base or DATA_API) + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
        return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _mark_rate_limited()
        elif e.code == 401 and not quiet_401:
            print('[Alpaca REST] 401 凭证无效，请检查 API Key/Secret')
        elif e.code not in (401, 404):   # 404=该代码无数据，属正常空态，不刷屏
            print(f'[Alpaca REST] HTTP {e.code}: {path}')
        return None
    except Exception as e:
        print(f'[Alpaca REST] 请求失败 {path}: {e}')
        return None


# ============================================================
# 昨收缓存（涨跌幅基准，snapshot 与日K预取都会更新它）
# ============================================================
_prev_close = {}                    # {symbol: 昨收/前收}
_prev_lock = threading.Lock()


def get_prev_close(symbol):
    with _prev_lock:
        return _prev_close.get(symbol.upper())


def _set_prev_close(symbol, value):
    if value is not None:
        with _prev_lock:
            _prev_close[symbol.upper()] = float(value)


def _parse_iso(ts):
    """解析 Alpaca ISO8601 时间戳（兼容结尾 Z 写法，Py3.10 的 fromisoformat 不认 Z）"""
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return None


def _same_day_bars(rows_desc):
    """按「最新一根所在的 ET 交易日」过滤降序 bar 列表（时间戳坏的直接跳过）"""
    kept = []
    day = None
    for bar in rows_desc:
        t = _parse_iso(bar.get('t')) if isinstance(bar, dict) else None
        if t is None:
            continue
        et_day = t.astimezone(TZ_ET).date()
        if day is None:
            day = et_day
        if et_day != day:
            break                       # 降序排列，往后都是更早的交易日
        kept.append(bar)
    return kept


def _close_of(bar):
    """取日K收盘价；坏数据返回 None。
    原先是 bars_desc[1]['c'] 裸下标，一根缺字段的 bar 会让整批美股快照抛出去，
    结果是「一只票的坏数据 → 所有美股这一轮全部没有行情」。"""
    try:
        return float(bar.get('c') or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None


def _resolve_prev_close(bars_desc, now_et=None):
    """从按时间降序排列的日K里解析「涨跌幅基准价」

    口径约定（与 A 股 yest_close 对齐）：
      - 最新日K是今天（b0.t 为 ET 今天）→ 今天已在交易，基准=昨天收盘（b1.c）
      - 最新日K不是今天（今天还没有 bar）：
          * 当前处于今天的数据窗口（盘前/白天刚开）→ 基准=最新日K收盘（b0.c，即昨收）
          * 窗口外（周末/深夜）→ 显示最近交易日的当日涨跌，基准=b1.c
    返回 (基准价, 最新日K收盘价)；bar 不足时基准可能为 None。
    """
    if not bars_desc:
        return None, None
    now_et = now_et or datetime.datetime.now(TZ_ET)
    b0 = bars_desc[0]
    t0 = _parse_iso(b0.get('t')) if isinstance(b0, dict) else None
    b0_is_today = bool(t0 and t0.astimezone(TZ_ET).date() == now_et.date())
    latest_close = _close_of(b0)
    prev_bar_close = _close_of(bars_desc[1]) if len(bars_desc) > 1 else None

    if b0_is_today:
        prev = prev_bar_close
    elif in_us_data_window(now_et):
        prev = latest_close                      # 盘前：报价对昨收
    else:
        prev = prev_bar_close or latest_close
    return prev, latest_close


def _fetch_daily_bars(codes):
    """批量拉最近 2 根日K（降序），返回 {symbol: [bar, ...]}，失败的不含在内"""
    out = {}
    # 必须显式传 start：实测不传 start 时 Alpaca 返回 bars=null（空），
    # 40 自然日足够覆盖 2 根日K（长假/停牌也够）
    start = (datetime.datetime.now(TZ_ET) - datetime.timedelta(days=40)).date().isoformat()
    for i in range(0, len(codes), SYMBOLS_PER_BATCH):
        chunk = codes[i:i + SYMBOLS_PER_BATCH]
        data = _http_get('/stocks/bars', {
            'symbols': ','.join(chunk),
            'timeframe': '1Day',
            'limit': 2 * len(chunk),          # 每股最多取 2 根（算昨收够用）
            'sort': 'desc',
            'feed': FEED,
            'adjustment': 'raw',
            'start': start,                   # 实测缺省 start 会返回空，必须给
        })
        bars_map = (data or {}).get('bars')
        if isinstance(bars_map, dict):
            for sym, rows in bars_map.items():
                if rows:
                    out[sym.upper()] = rows
    return out


def _fetch_latest_quotes(codes):
    """批量拉最新报价（含盘前盘后），返回 {symbol: quote_dict}"""
    out = {}
    for i in range(0, len(codes), SYMBOLS_PER_BATCH):
        chunk = codes[i:i + SYMBOLS_PER_BATCH]
        data = _http_get('/stocks/quotes/latest', {
            'symbols': ','.join(chunk),
            'feed': FEED,
        })
        q_map = (data or {}).get('quotes')
        if isinstance(q_map, dict):
            for sym, q in q_map.items():
                if q:
                    out[sym.upper()] = q
    return out


def pick_quote_price(bp, ap, last_trade=None, ref=None):
    """报价 → 可信现价（REST 与 WebSocket 共用的唯一口径）

    优先级：最近成交价 > 买卖中值（仅当价差够窄）> 单边价。
    价差过宽时返回 None，由调用方回退到最近收盘价 —— 盘前"有价无市"的中值
    只是挂单噪声，拿它算涨跌会显示假百分比甚至误触预警（见 MAX_SPREAD_RATIO）。
    ref 只用于判定价差宽窄，不参与取值。
    """
    try:
        bp = float(bp or 0)
        ap = float(ap or 0)
    except (TypeError, ValueError):
        return None
    try:
        lt = float(last_trade or 0)
    except (TypeError, ValueError):
        lt = 0
    if lt > 0:
        return round(lt, 4)
    if bp > 0 and ap > 0:
        mid = (bp + ap) / 2
        reference = float(ref or 0) if ref else 0
        if reference > 0 and abs(ap - bp) / reference > MAX_SPREAD_RATIO:
            return None                      # 宽价差：中值不可信
        return round(mid, 4)
    if bp > 0:
        return round(bp, 4)
    if ap > 0:
        return round(ap, 4)
    return None


def _quote_price(q, ref=None):
    """从最新报价里取价（口径见 pick_quote_price）"""
    if not isinstance(q, dict):
        return None
    return pick_quote_price(q.get('bp'), q.get('ap'), q.get('p'), ref)


# ============================================================
# 快照批量（休市兜底：最近收盘价 + 涨跌幅）
# ============================================================
_snap_lock = threading.Lock()
_snap_cache = {}                  # {symbol: snapshot}
_snap_ts = 0.0                    # 上次全量刷新时间


def latest_snapshot_batch(codes, force=False):
    """批量获取美股最新价快照（REST 兜底路径）

    返回 {code: {'price','yest_close','change_pct'}}，取不到的不含在结果里。
    TTL 内直接回缓存 —— 休市时价格不变，避免浪费免费版 200/分钟配额。
    force=True 时强制刷新（如 WebSocket 每次连上后要拿一次最新昨收）。
    """
    global _snap_ts
    codes = [c.upper() for c in codes if c]
    if not codes or not has_credentials():
        return {}

    now = time.time()
    with _snap_lock:
        fresh = (now - _snap_ts) < SNAPSHOT_TTL
        if fresh and all(c in _snap_cache for c in codes) and not force:
            return dict(_snap_cache)  # 缓存整体拷出（只会被读，不会被改）

    daily = _fetch_daily_bars(codes)
    quotes = _fetch_latest_quotes(codes)
    now_et = datetime.datetime.now(TZ_ET)
    in_window = in_us_data_window(now_et)      # 窗口内用实时报价，窗口外用收盘价

    updated = {}
    for sym in codes:
        bars = daily.get(sym)
        prev, latest_close = _resolve_prev_close(bars, now_et) if bars else (None, None)
        if prev is not None:
            _set_prev_close(sym, prev)

        price = _quote_price(quotes.get(sym) or {}, ref=latest_close)
        # 休市（数据窗口外）优先用最近收盘价（选项6-A）：
        # 盘后报价的买卖价差可能极宽（实测 AAPL 周五盘后 bp=328.76/ap=363.43，
        # 中值 346 严重偏离收盘 343.39），窗口外用中值会显示"错价"
        if not in_window and latest_close:
            price = latest_close
        if price is None:
            price = latest_close               # 窗口内但无实时报价 → 最近收盘价兜底
        if price is None:
            continue

        # 涨跌幅 = (现价 - 基准) / 基准；基准拿不到就留 None（渲染为 '--'）
        change_pct = None
        base = get_prev_close(sym)
        if base:
            change_pct = round((price - base) / base * 100, 2)

        updated[sym] = {
            'price': price,
            'yest_close': base,
            'change_pct': change_pct,
        }

    # 合并进缓存（失败的 symbol 保留旧值，避免网络抖动时价格闪烁为 '--'）
    with _snap_lock:
        _snap_cache.update(updated)
        _snap_ts = now
        if len(_snap_cache) > 300:              # 防无界增长
            for k in list(_snap_cache)[:-100]:
                _snap_cache.pop(k, None)
        return dict(_snap_cache)


# ============================================================
# 股票名称（Alpaca assets 接口，设置界面代码自动补名称用）
# ============================================================
_assets_cache = {}                 # {symbol: name} 进程内缓存
_assets_loaded = False
_assets_ts = 0.0
_assets_lock = threading.Lock()


def _load_assets(max_pages=6):
    """全量拉取美股资产列表（分页），建立 symbol → name 映射

    /v2/assets 属于交易 API（不在 data.alpaca.markets 上，打行情地址是 404）：
    免费版默认 paper 账户 → 先打 paper 地址；paper key 打实盘地址会 401，
    此时再兜底试实盘地址（覆盖用实盘账户的用户）。
    该接口不属于行情数据，不占 feed 口径限制；美股 active 约 1 万多只，
    limit=10000 约 2 页拉完。拉取失败保留旧缓存，下次调用重试。
    """
    global _assets_loaded, _assets_ts
    for base in (TRADING_API, LIVE_API):
        names = {}
        token = None
        for _ in range(max_pages):
            params = {'status': 'active', 'asset_class': 'us_equity', 'limit': 10000}
            if token:
                params['page_token'] = token
            # quiet_401：换 base 兜底时的 401 是地址不适用，不是凭证错误
            data = _http_get('/assets', params, timeout=10, base=base, quiet_401=True)
            if data is None:
                break                            # 该 base 不可用（如 paper key 打实盘），换下一个
            rows = data if isinstance(data, list) else []
            for a in rows:
                sym = (a or {}).get('symbol')
                nm = (a or {}).get('name')
                if sym and nm:
                    names[sym.upper()] = nm
            token = (data or {}).get('next_page_token') if isinstance(data, dict) else None
            if not rows or not token:
                break
        if names:
            with _assets_lock:
                _assets_cache.update(names)
                _assets_loaded = True
                _assets_ts = time.time()
            print(f'[Alpaca] 已加载 {len(names)} 只美股名称')
            return


def get_symbol_name(symbol, refresh_hours=24):
    """查美股名称；未命中缓存时懒加载 assets（每 24 小时最多全量刷一次）"""
    sym = (symbol or '').upper()
    if not sym or not has_credentials():
        return ''
    with _assets_lock:
        if sym in _assets_cache:
            return _assets_cache[sym]
        need_load = (not _assets_loaded) or (time.time() - _assets_ts > refresh_hours * 3600)
    if need_load:
        try:
            _load_assets()
        except Exception as e:
            print(f'[Alpaca] 加载股票名称失败: {e}')
    with _assets_lock:
        return _assets_cache.get(sym, '')


# ============================================================
# 历史 K 线（悬浮图数据源）
# ============================================================
def get_kline(code, period='1min', count=60):
    """获取美股历史 K 线（多周期），返回与 A 股一致的格式

    period: '1min' | 'daily' | 'weekly' → Alpaca 1Min / 1Day / 1Week
    数据源：Alpaca bars REST（feed=iex），desc 取最近 count 根后反转为升序
    时间统一转北京时间展示（与 A 股卡片的本地时间口径一致）。
    """
    if not has_credentials():
        return []
    symbol = (code or '').upper()
    timeframe = TIMEFRAME_MAP.get(period)
    if not timeframe:
        return []

    # 必须显式传 start：实测缺省 start 时 Alpaca bars 返回 null（空），K线会全空。
    # 按周期给回溯窗口（自然日），保证覆盖 count 根 + 长假/停牌余量
    start_days = {'1Min': 5, '1Day': 400, '1Week': 1500}[timeframe]
    start = (datetime.datetime.now(TZ_ET) - datetime.timedelta(days=start_days)).date().isoformat()

    data = _http_get(f'/stocks/{urllib.parse.quote(symbol)}/bars', {
        'timeframe': timeframe,
        'limit': min(count, 1000),
        'sort': 'desc',
        'feed': FEED,
        'adjustment': 'raw',
        'start': start,                         # 实测缺省 start 会返回空，必须给
    })
    rows = (data or {}).get('bars') or []
    if not rows:
        return []                               # 404（无数据）也走这里，静默降级

    if period == '1min':
        # 只保留最新一根所在的交易日：盘前今日不足 count 根时，desc 取数会把上一
        # 交易日的 bar 补进来，而标签只有 HH:MM → 图上出现两根同一时刻分属两天
        rows = _same_day_bars(rows)
        if not rows:
            return []

    # 日K顺手更新昨收缓存（挂机跨日时保持涨跌幅基准新鲜）
    if period == 'daily':
        prev, _ = _resolve_prev_close(rows)
        _set_prev_close(symbol, prev)

    out = []
    for r in rows:                              # rows 为降序，反转成升序画图
        t = _parse_iso(r.get('t'))
        if t is None:
            continue
        t_sh = t.astimezone(TZ_SH)
        if period == '1min':
            time_str = t_sh.strftime('%H:%M')
        else:
            time_str = t_sh.strftime('%m-%d')
        try:
            out.append({
                'time': time_str,
                'open': float(r.get('o') or 0),
                'high': float(r.get('h') or 0),
                'low': float(r.get('l') or 0),
                'close': float(r.get('c') or 0),
                'volume': float(r.get('v') or 0),
            })
        except (TypeError, ValueError):
            continue
    out.reverse()
    return out
