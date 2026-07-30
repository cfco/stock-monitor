#!/usr/bin/env python3
"""
price_alert.py — A股价格预警监控脚本 v2.0
用法示例:
  python price_alert.py --code sh601868 --target 2.90 --dir below
  python price_alert.py --code sz000560 --target 2.60 --dir above --interval 15
  python price_alert.py --config alert_config.json
  python price_alert.py --code sh601868 --target 2.87 --dir below --source tencent

数据源:
  tdx       通达信协议直连（默认，~15ms延迟，郑州节点）
  tencent   腾讯行情接口（备用，~155ms延迟）

参数:
  --code       股票代码 (如 sh601868, sz000560)
  --name       股票名称 (可选，自动从接口获取)
  --target     目标价格 (必填)
  --dir        方向: below=跌破提醒, above=涨破提醒, both=双向提醒
  --interval   检查间隔秒数 (默认5，配合tdx源15ms延迟足够)
  --source     数据源: tdx(默认) | tencent
  --host       自定义通达信服务器 (如 182.118.8.4:7709)
  --config     JSON配置文件路径 (可同时监控多只股票)
  --log        日志文件路径 (默认 reports/price_alert_日志.txt)
  --notify     提醒方式: popup(默认弹窗) | log(仅日志)
"""

import argparse
import json
import os
import sys
import time
import datetime
import subprocess
import threading

SCRIPT_DIR = None

# ============================================================
# TDX节点配置（多节点故障转移）
# ============================================================

# 节点列表（按优先级排序，老板优选低延迟节点）
TDX_NODES = [
    {'id': 'zhengzhou_z1', 'name': '云行情郑州主站Z1', 'host': '182.118.8.4:7709', 'priority': 1},
    {'id': 'shanghai_z1', 'name': '云行情上海主站Z1', 'host': '114.141.177.44:7709', 'priority': 2},
    {'id': 'beijing_z1', 'name': '云行情北京主站Z1', 'host': '123.125.108.103:7709', 'priority': 3},
]

# 默认节点（郑州主站，延迟最低）
DEFAULT_TDX_HOST = '182.118.8.4:7709'
DEFAULT_TDX_NODE_NAME = '云行情郑州主站Z1'

# 全局eltdx客户端（连接复用）
_tdx_client = None
_current_node = None
_failed_nodes = set()


def get_tdx_client(host=None):
    """获取或创建eltdx客户端（单例，连接复用，支持故障转移）"""
    global _tdx_client, _current_node

    # 如果指定了host且当前连接不是该host，重新连接
    if host and _current_node != host:
        close_tdx_client()

    if _tdx_client is not None:
        return _tdx_client

    from eltdx import Client

    # 如果指定了host，直接连接
    if host:
        _tdx_client = Client(host=host)
        _current_node = host
        return _tdx_client

    # 否则按优先级选择节点
    for node in TDX_NODES:
        if node['host'] in _failed_nodes:
            continue
        try:
            _tdx_client = Client(host=node['host'])
            _current_node = node['host']
            print(f'[TDX] Connected to {node["name"]} ({node["host"]})')
            return _tdx_client
        except Exception as e:
            print(f'[TDX] Failed to connect {node["name"]}: {e}')
            _failed_nodes.add(node['host'])

    # 所有节点都失败，重置并重试第一个
    _failed_nodes.clear()
    raise ConnectionError('All TDX nodes failed')


def close_tdx_client():
    """关闭eltdx连接"""
    global _tdx_client, _current_node
    if _tdx_client is not None:
        try:
            _tdx_client.close()
        except Exception:
            pass
        _tdx_client = None
        _current_node = None


def get_current_node_info():
    """获取当前连接的节点信息"""
    if _current_node:
        for node in TDX_NODES:
            if node['host'] == _current_node:
                return node
    return {'id': 'unknown', 'name': '未连接', 'host': 'N/A'}


def get_available_nodes():
    """获取所有可用节点列表"""
    return [n for n in TDX_NODES if n['host'] not in _failed_nodes]


def show_popup(title, message):
    """Windows本地弹窗通知（零依赖，ctypes调用原生API）"""
    try:
        import ctypes
        # MB_ICONWARNING = 0x30, MB_OK = 0x00
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x30)
    except Exception:
        # 如果MessageBox不可用，回退到PowerShell弹窗
        try:
            ps_cmd = (
                f'Add-Type -AssemblyName System.Windows.Forms; '
                f'[System.Windows.Forms.MessageBox]::Show("{message}", "{title}", '
                f'[System.Windows.Forms.MessageBoxButtons]::OK, '
                f'[System.Windows.Forms.MessageBoxIcon]::Warning)'
            )
            subprocess.Popen(
                ['powershell', '-NoProfile', '-Command', ps_cmd],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception:
            print(f'[POPUP FAILED] {title}: {message}')


def show_popup_async(title, message):
    """异步弹窗（不阻塞监控循环）"""
    t = threading.Thread(target=show_popup, args=(title, message), daemon=True)
    t.start()


_tdx_lock = threading.Lock()


def get_quote_tdx(code, host=None):
    """从通达信协议获取实时行情（郑州节点，~15ms延迟）"""
    with _tdx_lock:
        client = get_tdx_client(host)
        result = client.get_quote(code)
    if not result or len(result) == 0:
        raise ValueError(f'未获取到数据: {code}')

    q = result[0]
    # 计算涨跌幅
    pre_close = q.pre_close_price
    last_price = q.last_price
    change_pct = None
    if pre_close and pre_close > 0:
        change_pct = ((last_price - pre_close) / pre_close) * 100

    return {
        'name': code,  # tdx协议不返回名称，用code
        'code': code,
        'price': last_price,
        'yest_close': pre_close,
        'open': q.open_price,
        'high': q.high_price,
        'low': q.low_price,
        'volume': q.total_hand,
        'amount': q.amount,
        'change_pct': change_pct,
        # 盘口
        'bid1': q.buy_levels[0].price if q.buy_levels else None,
        'bid1_vol': q.buy_levels[0].volume if q.buy_levels else None,
        'ask1': q.sell_levels[0].price if q.sell_levels else None,
        'ask1_vol': q.sell_levels[0].volume if q.sell_levels else None,
    }


def get_quote_tencent(code):
    """从腾讯接口获取实时行情（备用，~155ms延迟）
    支持 A股(sh/sz)、港股(hk)、美股(us)。
    注意：美股返回字段顺序与A股不同，本函数自动适配。
    """
    import urllib.request
    # 美股代码统一加 us 前缀（兼容用户写 usAAPL 或直接 AAPL）
    if not code.lower().startswith(('sh', 'sz', 'hk', 'us', 's_sh', 's_sz', 's_hk', 's_us')):
        # 字母代码默认视为美股
        if code.isalpha():
            code = 'us' + code
    url = f'https://qt.gtimg.cn/q={code}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode('gbk')

    # 美股返回格式与A股不同：字段数少，无~分隔的完整结构
    # 格式示例：v_usAAPL="100~Apple~AAPL~170.50~169.30~171.00~..."; 末尾有涨跌幅等
    is_us = code.lower().startswith('us') or code.lower().startswith('s_us')
    f = content.split('~')

    if is_us and len(f) < 10:
        # 美股精简格式，尝试正则提取
        import re
        m = re.search(r'"([^"]+)"', content)
        if not m:
            raise ValueError(f'腾讯接口返回格式异常: {content[:100]}')
        parts = m.group(1).split('~')
        # 美股典型字段：marketCode~name~symbol~price~prevClose~open~high~low~...~changePct
        def _safe_float(idx):
            try:
                return float(parts[idx]) if idx < len(parts) and parts[idx] not in ('', '0.00') else None
            except (ValueError, IndexError):
                return None
        price = _safe_float(3)
        prev_close = _safe_float(4)
        change_pct = _safe_float(32) if len(parts) > 32 else None
        # 美股涨跌幅可能字段位置不同，如果取不到，用 price/prev_close 计算
        if change_pct is None and price and prev_close and prev_close > 0:
            change_pct = ((price - prev_close) / prev_close) * 100
        return {
            'name': parts[1] if len(parts) > 1 else code,
            'code': parts[2] if len(parts) > 2 else code,
            'price': price,
            'yest_close': prev_close,
            'open': _safe_float(5),
            'high': _safe_float(33) if len(parts) > 33 else None,
            'low': _safe_float(34) if len(parts) > 34 else None,
            'change_pct': change_pct,
        }

    # A股/港股标准格式
    return {
        'name': f[1],
        'code': f[2],
        'price': float(f[3]),
        'yest_close': float(f[4]),
        'open': float(f[5]) if f[5] else None,
        'high': float(f[33]) if len(f) > 33 and f[33] else None,
        'low': float(f[34]) if len(f) > 34 and f[34] else None,
        'change_pct': float(f[32]) if len(f) > 32 and f[32] else None,
    }


def _detect_trading_session(meta):
    """根据 Yahoo meta 信息判断当前交易时段
    返回: 'pre' | 'regular' | 'post' | 'closed'
    """
    import time as _time
    now = _time.time()
    periods = meta.get('currentTradingPeriod', {})
    if not periods:
        return 'closed'
    # 检查盘前
    pre = periods.get('pre', {})
    if pre and pre.get('start') and pre.get('end'):
        if pre['start'] <= now <= pre['end']:
            return 'pre'
    # 检查常规
    reg = periods.get('regular', {})
    if reg and reg.get('start') and reg.get('end'):
        if reg['start'] <= now <= reg['end']:
            return 'regular'
    # 检查盘后
    post = periods.get('post', {})
    if post and post.get('start') and post.get('end'):
        if post['start'] <= now <= post['end']:
            return 'post'
    return 'closed'


def get_quote_yahoo(code):
    """从 Yahoo Finance 获取实时行情（支持全球市场：韩股/美股/港股/欧股等）
    韩股代码格式：005930.KS (KOSPI) / 052690.KQ (KOSDAQ)
    美股代码格式：AAPL / TSLA
    港股代码格式：0700.HK

    自动识别盘前/盘后交易时段，返回对应价格。
    """
    import urllib.request
    import json as _json
    # 自动加韩股后缀：kr005930 → 005930.KS
    symbol = code
    if code.lower().startswith('kr'):
        num = code[2:]
        symbol = f'{num}.KS' if num.startswith('0') and len(num) == 6 else f'{num}.KQ'

    # includePrePost=true 包含盘前盘后数据点
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=2m&range=1d&includePrePost=true'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = _json.loads(resp.read().decode('utf-8'))

    result = data.get('chart', {}).get('result')
    if not result:
        raise ValueError(f'Yahoo接口未返回数据: {symbol}')
    meta = result[0].get('meta', {})
    indicators = result[0].get('indicators', {})
    quotes = indicators.get('quotes', [{}])[0]
    timestamps = result[0].get('timestamp', [])

    # 检测交易时段，选择最合适的价格
    session = _detect_trading_session(meta)
    session_label = {
        'pre': '盘前', 'regular': '常规', 'post': '盘后', 'closed': '已休市'
    }.get(session, '')

    # 各时段价格
    pre_price = meta.get('preMarketPrice')
    post_price = meta.get('postMarketPrice')
    reg_price = meta.get('regularMarketPrice')

    # 如果在盘前/盘后但 meta 中没有对应价格，从 chart 数据点中提取
    if session in ('pre', 'post'):
        latest_price = _get_latest_chart_price(timestamps, quotes, meta, session)
        if session == 'pre' and not pre_price:
            pre_price = latest_price
        if session == 'post' and not post_price:
            post_price = latest_price
        # 如果有盘前盘后价格，优先使用
        if session == 'pre' and pre_price:
            price = pre_price
        elif session == 'post' and post_price:
            price = post_price
        else:
            price = reg_price
    else:
        price = reg_price

    prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
    change_pct = None
    if price and prev_close and prev_close > 0:
        change_pct = ((price - prev_close) / prev_close) * 100

    # 从日内报价数组取 open/high/low
    opens = quotes.get('open') or []
    highs = quotes.get('high') or []
    lows = quotes.get('low') or []
    valid_open = next((v for v in opens if v is not None), None)
    valid_high = max([v for v in highs if v is not None], default=None)
    valid_low = min([v for v in lows if v is not None], default=None)

    week52_high = meta.get('fiftyTwoWeekHigh')
    week52_low = meta.get('fiftyTwoWeekLow')

    # Parse klines from chart data (already fetched, no extra request)
    klines = []
    closes_list = quotes.get('close') or []
    if timestamps and closes_list:
        for i in range(len(timestamps)):
            o = opens[i] if i < len(opens) else None
            h = highs[i] if i < len(highs) else None
            l = lows[i] if i < len(lows) else None
            c = closes_list[i] if i < len(closes_list) else None
            v = (quotes.get('volume') or [])
            vol = v[i] if i < len(v) else 0
            if o is not None and h is not None and l is not None and c is not None:
                klines.append({
                    'time': datetime.datetime.fromtimestamp(timestamps[i]).strftime('%H:%M'),
                    'open': o, 'high': h, 'low': l, 'close': c, 'volume': vol or 0,
                })

    return {
        'name': meta.get('symbol', symbol),
        'code': symbol,
        'price': price,
        'yest_close': prev_close,
        'open': valid_open,
        'high': valid_high,
        'low': valid_low,
        'change_pct': change_pct,
        'currency': meta.get('currency', ''),
        'session': session_label,
        'session_raw': session,
        'pre_market_price': pre_price,
        'post_market_price': post_price,
        'week52_high': week52_high,
        'week52_low': week52_low,
        'klines': klines,
    }


def _get_latest_chart_price(timestamps, quotes, meta, target_session):
    """从 chart 数据点中提取盘前/盘后的最新价格"""
    closes = quotes.get('close') or []
    if not timestamps or not closes:
        return None

    ctp = meta.get('currentTradingPeriod', {})
    period = ctp.get(target_session, {})
    if not period:
        return None

    start = period.get('start', 0)
    end = period.get('end', 0)

    # 从后往前找在目标时段内的最后一个有效价格
    for i in range(len(timestamps) - 1, -1, -1):
        ts = timestamps[i]
        close = closes[i] if i < len(closes) else None
        if close is not None and start <= ts <= end:
            return close

    return None


def detect_market(code):
    """根据代码格式自动识别市场
    返回: 'a_share' | 'hk' | 'us' | 'kr' | 'yahoo' | 'unknown'
    """
    c = code.lower()
    if c.startswith(('sh', 'sz')):
        return 'a_share'
    if c.startswith('hk'):
        return 'hk'
    if c.startswith('us') and len(c) > 2 and not c[2:].isdigit():
        return 'us'
    if c.startswith('kr'):
        return 'kr'
    # 直接 Yahoo 格式（包含点号）：005930.KS / AAPL / 0700.HK
    if '.' in code:
        return 'yahoo'
    # 纯字母视为美股
    if code.isalpha():
        return 'us'
    return 'unknown'


def get_quote(code, source='auto', host=None):
    """统一行情获取接口（自动路由）
    source:
      auto    - 根据代码格式自动选择（默认）
      tdx     - 强制通达信协议（仅A股）
      tencent - 强制腾讯接口（A股/港股/美股）
      yahoo   - 强制Yahoo Finance（全球市场，推荐韩股）
    """
    if source == 'tdx':
        return get_quote_tdx(code, host)
    if source == 'tencent':
        return get_quote_tencent(code)
    if source == 'yahoo':
        return get_quote_yahoo(code)

    # auto: 根据市场路由
    market = detect_market(code)
    if market == 'a_share':
        # A股优先 TDX（低延迟），失败回退腾讯
        try:
            return get_quote_tdx(code, host)
        except Exception:
            return get_quote_tencent(code)
    if market == 'us':
        # 美股：盘前/盘后时段优先 Yahoo（支持盘前盘后数据），常规时段用腾讯（更快）
        try:
            q_yahoo = get_quote_yahoo(code)
            session = q_yahoo.get('session_raw', '')
            if session in ('pre', 'post'):
                # 盘前/盘后：Yahoo 数据更准确
                return q_yahoo
            # 常规交易或休市：用腾讯接口（更快），但如果腾讯失败则用 Yahoo
            try:
                return get_quote_tencent(code)
            except Exception:
                return q_yahoo
        except Exception:
            return get_quote_tencent(code)
    if market == 'hk':
        # 港股：腾讯接口即可
        return get_quote_tencent(code)
    if market in ('kr', 'yahoo'):
        # 韩股/其他市场：Yahoo Finance
        return get_quote_yahoo(code)
    # 未知市场，尝试腾讯
    return get_quote_tencent(code)


def resolve_name(code, source='tdx', host=None):
    """获取股票名称（tdx协议不返回名称，用腾讯接口补充）"""
    try:
        q = get_quote_tencent(code)
        return q.get('name', code)
    except Exception:
        return code




def get_quotes_tencent_batch(codes):
    """腾讯接口批量获取行情（一次请求多只股票）
    codes: 代码列表，如 ['sh000001', 'sh601398', 'usKORU']
    返回: {code: quote_dict, ...}，失败的代码不包含在结果中
    """
    import urllib.request
    import re

    # 统一美股代码前缀
    normalized = []
    for code in codes:
        c = code.lower()
        if not c.startswith(('sh', 'sz', 'hk', 'us', 's_sh', 's_sz', 's_hk', 's_us')):
            if code.isalpha():
                normalized.append('us' + code)
            else:
                normalized.append(code)
        else:
            normalized.append(code)

    # 批量请求：逗号分隔，腾讯主站（超时3秒快速失败以便回退到其他源）
    codes_str = ','.join(normalized)
    url = f'https://qt.gtimg.cn/q={codes_str}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            content = resp.read().decode('gbk')
    except Exception as e:
        print(f'[Tencent Batch] failed: {e}')
        return {}

    # 解析返回的多条数据（每条以 v_ 开头，分号分隔）
    results = {}
    # 匹配 v_xxx="content" 格式
    pattern = r'v_[^=]+="([^"]*)"'
    matches = re.findall(pattern, content)

    # 建立原始代码到标准化代码的映射
    code_map = {}
    for i, code in enumerate(codes):
        code_map[code] = normalized[i]

    for match_str in matches:
        f = match_str.split('~')
        if len(f) < 5:
            continue

        # 提取返回的代码
        ret_code = f[2] if len(f) > 2 else ''
        # 判断是否美股
        is_us = ret_code.lower().startswith('us') or any(
            n.lower().startswith('us') and n.upper().endswith(ret_code.upper())
            for n in normalized
        )

        # 美股精简格式
        if is_us and len(f) < 10:
            def _safe_float(idx, parts=f):
                try:
                    return float(parts[idx]) if idx < len(parts) and parts[idx] not in ('', '0.00') else None
                except (ValueError, IndexError):
                    return None
            price = _safe_float(3)
            prev_close = _safe_float(4)
            change_pct = _safe_float(32) if len(f) > 32 else None
            if change_pct is None and price and prev_close and prev_close > 0:
                change_pct = ((price - prev_close) / prev_close) * 100
            quote = {
                'name': f[1] if len(f) > 1 else ret_code,
                'code': ret_code,
                'price': price,
                'yest_close': prev_close,
                'open': _safe_float(5),
                'high': _safe_float(33) if len(f) > 33 else None,
                'low': _safe_float(34) if len(f) > 34 else None,
                'change_pct': change_pct,
            }
        else:
            # A股/港股标准格式
            try:
                quote = {
                    'name': f[1],
                    'code': f[2],
                    'price': float(f[3]) if f[3] else None,
                    'yest_close': float(f[4]) if f[4] else None,
                    'open': float(f[5]) if len(f) > 5 and f[5] else None,
                    'high': float(f[33]) if len(f) > 33 and f[33] else None,
                    'low': float(f[34]) if len(f) > 34 and f[34] else None,
                    'change_pct': float(f[32]) if len(f) > 32 and f[32] else None,
                }
            except (ValueError, IndexError) as e:
                continue

        # 将结果映射回原始代码
        # 先尝试精确匹配
        matched = False
        for orig_code, norm_code in code_map.items():
            if orig_code not in results:
                if norm_code == ret_code or norm_code.upper().endswith(ret_code.upper()):
                    if quote.get('price') is not None:
                        results[orig_code] = quote
                        matched = True
                        break
        # 如果没匹配上，用 ret_code 反查
        if not matched and ret_code:
            for orig_code in codes:
                if orig_code not in results:
                    results[orig_code] = quote
                    break

    return results



def get_quotes_sina_batch(codes):
    """新浪接口批量获取A股/港股行情（第二数据源）
    新浪需要 Referer 头，否则返回403。
    返回格式: var hq_str_sh601398="名称,今开,昨收,当前价,最高,最低,...";
    """
    import urllib.request
    import re as _re

    # 新浪支持 A股(sh/sz) 和港股(hk)
    sina_codes = []
    for code in codes:
        c = code.lower()
        if c.startswith(('sh', 'sz', 'hk', 's_sh', 's_sz', 's_hk')):
            sina_codes.append(code)
    if not sina_codes:
        return {}

    codes_str = ','.join(sina_codes)
    url = f'https://hq.sinajs.cn/list={codes_str}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.sina.com.cn'
    })
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            text = resp.read().decode('gbk')
    except Exception as e:
        print(f'[Sina Batch] failed: {e}')
        return {}

    results = {}
    for line in text.strip().split('\n'):
        line = line.strip()
        if '=' not in line:
            continue
        prefix, value = line.split('=', 1)
        # hq_str_sh601398 → sh601398
        code_part = prefix.replace('var hq_str_', '').strip()
        if not code_part:
            continue
        m = _re.search(r'"([^"]*)"', value)
        if not m:
            continue
        fields = m.group(1).split(',')
        if len(fields) < 6:
            continue
        try:
            name = fields[0]
            open_price = float(fields[1]) if fields[1] else None
            yest_close = float(fields[2]) if fields[2] else None
            price = float(fields[3]) if fields[3] else None
            high = float(fields[4]) if fields[4] else None
            low = float(fields[5]) if fields[5] else None
            change_pct = None
            if price is not None and yest_close and yest_close > 0:
                change_pct = ((price - yest_close) / yest_close) * 100
            results[code_part] = {
                'name': name,
                'code': code_part,
                'price': price,
                'yest_close': yest_close,
                'open': open_price,
                'high': high,
                'low': low,
                'change_pct': change_pct,
            }
        except (ValueError, IndexError):
            continue
    return results


def get_quotes_eastmoney_batch(codes):
    """东方财富批量获取A股行情（第三数据源）
    secid格式: 1.{code}(沪市) / 0.{code}(深市)
    f2=最新价 f3=涨跌幅 f12=代码 f13=市场 f14=名称 f15=最高 f16=最低 f17=今开 f18=昨收
    """
    import urllib.request
    import json as _json

    secids = []
    for code in codes:
        c = code.lower()
        if c.startswith('sh'):
            secids.append(f'1.{c[2:]}')
        elif c.startswith('sz'):
            secids.append(f'0.{c[2:]}')
        # 东方财富不处理美股/港股
    if not secids:
        return {}

    secids_str = ','.join(secids)
    # fltt=2 返回浮点价格；用 http 避免 https 握手被拒
    url = (
        f'http://push2.eastmoney.com/api/qt/ulist.np/get'
        f'?fltt=2&fields=f2,f3,f12,f13,f14,f15,f16,f17,f18'
        f'&secids={secids_str}'
    )
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://quote.eastmoney.com/'
    })
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[EastMoney Batch] failed: {e}')
        return {}

    diff = data.get('data', {}).get('diff', [])
    if not diff:
        return {}

    def _safe_float(val):
        if val is None or val == '-' or val == '':
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    results = {}
    for item in diff:
        secid = str(item.get('f12', ''))
        market = item.get('f13', 0)
        prefix = 'sh' if market == 1 else 'sz'
        orig_code = f'{prefix}{secid}'
        price = _safe_float(item.get('f2'))
        if price is None:
            continue
        results[orig_code] = {
            'name': item.get('f14', orig_code),
            'code': orig_code,
            'price': price,
            'yest_close': _safe_float(item.get('f18')),
            'open': _safe_float(item.get('f17')),
            'high': _safe_float(item.get('f15')),
            'low': _safe_float(item.get('f16')),
            'change_pct': _safe_float(item.get('f3')),
        }
    return results


def get_quotes_batch(codes, source='auto', host=None):
    """统一批量行情获取接口
    按数据源分组，减少请求次数：
    - A股/港股：腾讯批量请求（1次）
    - 美股：Yahoo Finance 并发（N次，但可并发）
    - 全tdx：批量请求
    返回: {code: quote_dict, ...}
    """
    if not codes:
        return {}

    # 强制指定数据源
    if source == 'tencent':
        return get_quotes_tencent_batch(codes)

    if source == 'tdx':
        results = {}
        for code in codes:
            try:
                results[code] = get_quote_tdx(code, host)
            except Exception as e:
                print(f'[TDX Batch] Error for {code}: {e}')
        return results

    if source == 'yahoo':
        results = {}
        for code in codes:
            try:
                results[code] = get_quote_yahoo(code)
            except Exception as e:
                print(f'[Yahoo Batch] Error for {code}: {e}')
        return results

    # auto: 按市场分组
    a_share_codes = []
    us_codes = []
    hk_codes = []
    kr_codes = []
    other_codes = []

    for code in codes:
        market = detect_market(code)
        if market == 'a_share':
            a_share_codes.append(code)
        elif market == 'us':
            us_codes.append(code)
        elif market == 'hk':
            hk_codes.append(code)
        elif market in ('kr', 'yahoo'):
            kr_codes.append(code)
        else:
            other_codes.append(code)

    results = {}

    # A股：三源轮询（腾讯/新浪/东方财富），避免单一接口限流
    if a_share_codes:
        a_sources = ['tencent', 'sina', 'eastmoney']
        idx = getattr(get_quotes_batch, '_a_share_idx', 0)
        cur_source = a_sources[idx % len(a_sources)]
        get_quotes_batch._a_share_idx = (idx + 1) % len(a_sources)

        batch_results = {}
        if cur_source == 'tencent':
            batch_results = get_quotes_tencent_batch(a_share_codes)
        elif cur_source == 'sina':
            batch_results = get_quotes_sina_batch(a_share_codes)
        elif cur_source == 'eastmoney':
            batch_results = get_quotes_eastmoney_batch(a_share_codes)

        # 当前源获取不全的，用腾讯补充
        missing = [c for c in a_share_codes
                   if c not in batch_results or batch_results[c].get('price') is None]
        if missing:
            tencent_fill = get_quotes_tencent_batch(missing)
            for c in missing:
                if c in tencent_fill and tencent_fill[c].get('price') is not None:
                    batch_results[c] = tencent_fill[c]

        results.update(batch_results)

    # 港股：腾讯批量
    if hk_codes:
        results.update(get_quotes_tencent_batch(hk_codes))

    # 美股：先检测交易时段（Yahoo），常规时段用腾讯批量
    if us_codes:
        # 腾讯批量获取（一次请求，快）
        tencent_us = get_quotes_tencent_batch(us_codes)
        # Yahoo 并发获取（检测交易时段 + 盘前盘后数据）
        yahoo_us = {}
        from concurrent.futures import ThreadPoolExecutor
        def _fetch_yahoo(code):
            try:
                return code, get_quote_yahoo(code)
            except Exception:
                return code, None
        with ThreadPoolExecutor(max_workers=min(8, len(us_codes))) as ex:
            for code, q in ex.map(_fetch_yahoo, us_codes):
                if q is not None:
                    yahoo_us[code] = q
        # 合并：Yahoo 优先（有交易时段信息），腾讯补充
        for code in us_codes:
            yq = yahoo_us.get(code)
            tq = tencent_us.get(code)
            if yq:
                session = yq.get('session_raw', '')
                if session in ('pre', 'post'):
                    # 盘前盘后用 Yahoo 数据
                    results[code] = yq
                else:
                    # 常规：用腾讯（更快），补充 Yahoo 的交易时段
                    if tq and tq.get('price') is not None:
                        tq['session'] = yq.get('session', '')
                        tq['session_raw'] = yq.get('session_raw', '')
                        tq['currency'] = yq.get('currency', 'USD')
                        results[code] = tq
                    else:
                        results[code] = yq
            elif tq and tq.get('price') is not None:
                results[code] = tq

    # 韩股/其他：Yahoo 并发
    if kr_codes:
        from concurrent.futures import ThreadPoolExecutor
        def _fetch_yahoo_kr(code):
            try:
                return code, get_quote_yahoo(code)
            except Exception:
                return code, None
        with ThreadPoolExecutor(max_workers=min(8, len(kr_codes))) as ex:
            for code, q in ex.map(_fetch_yahoo_kr, kr_codes):
                if q is not None:
                    results[code] = q

    # 其他：腾讯批量
    if other_codes:
        results.update(get_quotes_tencent_batch(other_codes))

    return results


def get_kline_1min(code, count=60):
    """获取1分钟K线数据
    A股: 东方财富接口
    美股/韩股: Yahoo Finance
    返回: [{'time': 'HH:MM', 'open': x, 'high': x, 'low': x, 'close': x, 'volume': x}, ...]
    """
    market = detect_market(code)
    if market == 'a_share':
        return _get_kline_eastmoney(code, count)
    elif market in ('us', 'kr', 'yahoo', 'hk'):
        return _get_kline_yahoo(code, count)
    return []


def _get_kline_eastmoney(code, count=60):
    """东方财富1分钟K线 (A股)
    klt=1 表示1分钟, fqt=1 前复权
    """
    import urllib.request
    import json as _json

    c = code.lower()
    if c.startswith('sh'):
        secid = f'1.{c[2:]}'
    elif c.startswith('sz'):
        secid = f'0.{c[2:]}'
    else:
        return []

    url = (
        f'http://push2his.eastmoney.com/api/qt/stock/kline/get'
        f'?secid={secid}&klt=1&fqt=1&end=20500101&lmt={count}'
        f'&fields1=f1,f2,f3,f5,f6'
        f'&fields2=f51,f52,f53,f54,f55,f56'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[KLine EastMoney] failed: {e}')
        return []

    klines = data.get('data', {}).get('klines', [])
    if not klines:
        return []

    result = []
    for line in klines:
        parts = line.split(',')
        if len(parts) < 6:
            continue
        try:
            # f51=时间, f52=开, f53=收, f54=高, f55=低, f56=量
            time_str = parts[0].split(' ')[-1] if ' ' in parts[0] else parts[0]
            result.append({
                'time': time_str,
                'open': float(parts[1]),
                'close': float(parts[2]),
                'high': float(parts[3]),
                'low': float(parts[4]),
                'volume': float(parts[5]),
            })
        except (ValueError, IndexError):
            continue
    return result


def _get_kline_yahoo(code, count=60):
    """Yahoo Finance 1分钟K线 (美股/韩股)
    range=1h 约60根1分钟K线
    """
    import urllib.request
    import json as _json
    import time as _time

    symbol = code
    if code.lower().startswith('kr'):
        num = code[2:]
        symbol = f'{num}.KS' if num.startswith('0') and len(num) == 6 else f'{num}.KQ'

    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1h'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[KLine Yahoo] failed: {e}')
        return []

    result_obj = data.get('chart', {}).get('result')
    if not result_obj:
        return []

    timestamps = result_obj[0].get('timestamp', [])
    indicators = result_obj[0].get('indicators', {})
    quotes = indicators.get('quote', [{}])[0]

    opens = quotes.get('open', [])
    highs = quotes.get('high', [])
    lows = quotes.get('low', [])
    closes = quotes.get('close', [])
    volumes = quotes.get('volume', [])

    result = []
    for i in range(min(len(timestamps), count)):
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        v = volumes[i] if i < len(volumes) else 0
        if o is None or c is None or h is None or l is None:
            continue
        time_str = _time.strftime('%H:%M', _time.localtime(timestamps[i]))
        result.append({
            'time': time_str,
            'open': o, 'high': h, 'low': l, 'close': c,
            'volume': v or 0,
        })
    return result


def check_single(code, name, target, direction, interval, log_file,
                 source='tdx', host=None, notify='popup'):
    """监控单只股票"""
    source_label = f'TDX({host or DEFAULT_TDX_HOST})' if source == 'tdx' else 'Tencent'
    print(f'========================================')
    print(f'  PRICE ALERT MONITOR v2.0')
    print(f'  {name} ({code})')
    if direction == 'below':
        print(f'  Condition: price <= {target}')
    elif direction == 'above':
        print(f'  Condition: price >= {target}')
    else:
        print(f'  Condition: price <= {target} OR price >= {target}')
    print(f'  Source: {source_label} | Interval: {interval}s')
    print(f'  Started: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'========================================')
    print()

    alert_below = direction in ('below', 'both')
    alert_above = direction in ('above', 'both')
    check_count = 0

    while True:
        try:
            q = get_quote(code, source, host)
            price = q['price']
            if price is None:
                print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] Price is None, skipping...')
                time.sleep(interval)
                continue
            name = q['name']
            now = datetime.datetime.now().strftime('%H:%M:%S')
            check_count += 1

            triggered = False
            reasons = []

            if alert_below and price <= target:
                triggered = True
                reasons.append(f'price {price:.2f} <= target {target}')
            if alert_above and price >= target:
                triggered = True
                reasons.append(f'price {price:.2f} >= target {target}')

            if triggered:
                diff = price - target
                direction_cn = '跌破' if diff <= 0 else '涨破'
                pct_str = f'{q["change_pct"]:+.2f}%' if q.get('change_pct') is not None else 'N/A'
                alert_line = (
                    f'[{now}] *** ALERT *** {name}({code}) '
                    f'{direction_cn} {target} | 当前价: {price:.2f} | '
                    f'涨跌: {pct_str}'
                )
                print(alert_line)
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(alert_line + '\n')
                # 弹窗通知
                if notify == 'popup':
                    popup_msg = (
                        f'{name}({code}) {direction_cn} {target}\n'
                        f'当前价: {price:.2f}\n'
                        f'涨跌幅: {pct_str}\n'
                        f'时间: {now}'
                    )
                    show_popup_async(f'价格预警 - {name}', popup_msg)
                # 触发后继续监控，但降低频率到60秒
                print(f'[{now}] Alert saved. Continuing monitor at 60s interval...')
                time.sleep(60)
                continue
            else:
                diff = price - target
                pct_str = f'{q["change_pct"]:+.2f}%' if q.get('change_pct') is not None else ''
                status = f'diff: {abs(diff):.2f}'
                if pct_str:
                    status += f' | {pct_str}'
                print(f'[{now}] #{check_count} {name} = {price:.2f} ({status}) [OK]')

        except Exception as e:
            now = datetime.datetime.now().strftime('%H:%M:%S')
            print(f'[{now}] Error: {e}')
            # tdx连接失败时尝试重连
            if source == 'tdx':
                close_tdx_client()
            time.sleep(interval)
            continue

        time.sleep(interval)


def check_multi(config_file, source='tdx', host=None, notify='popup'):
    """从JSON配置文件监控多只股票"""
    with open(config_file, 'r', encoding='utf-8') as f:
        config = json.load(f)

    interval = config.get('interval', 5)
    log_file = config.get('log', get_log_path('multi'))
    items = config.get('alerts', [])

    source_label = f'TDX({host or DEFAULT_TDX_HOST})' if source == 'tdx' else 'Tencent'
    print(f'========================================')
    print(f'  MULTI-STOCK PRICE ALERT MONITOR v2.0')
    print(f'  Monitoring {len(items)} stock(s)')
    print(f'  Source: {source_label} | Interval: {interval}s')
    print(f'  Started: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'========================================')
    print()

    # 记录已触发的预警（避免重复刷屏，每60秒冷却）
    alert_cooldown = {}

    while True:
        now_str = datetime.datetime.now().strftime('%H:%M:%S')

        for item in items:
            code = item['code']
            target = item['target']
            direction = item.get('dir', 'below')
            name = item.get('name', code)
            alert_below = direction in ('below', 'both')
            alert_above = direction in ('above', 'both')

            try:
                q = get_quote(code, source, host)
                price = q.get('price')
                if price is None:
                    print(f'[{now_str}] {name}({code}) Price is None, skipping')
                    continue
                name = q.get('name', name)

                triggered = False
                reason = ''

                if alert_below and price <= target:
                    triggered = True
                    reason = f'{name}({code}) 跌破 {target} | 当前: {price:.2f}'
                if alert_above and price >= target:
                    triggered = True
                    reason = f'{name}({code}) 涨破 {target} | 当前: {price:.2f}'

                if triggered:
                    # 冷却检查
                    cooldown_key = f'{code}_{direction}'
                    last_alert_time = alert_cooldown.get(cooldown_key, 0)
                    current_time = time.time()

                    if current_time - last_alert_time >= 60:
                        alert_line = f'[{now_str}] *** ALERT *** {reason}'
                        print(alert_line)
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(alert_line + '\n')
                        alert_cooldown[cooldown_key] = current_time
                        if notify == 'popup':
                            show_popup_async('价格预警', reason)
                else:
                    diff = price - target
                    print(f'[{now_str}] {name} = {price:.2f} (target: {target}, diff: {abs(diff):.2f})')

            except Exception as e:
                print(f'[{now_str}] {name}({code}) Error: {e}')
                if source == 'tdx':
                    close_tdx_client()

        time.sleep(interval)


def get_log_path(label):
    """生成日志文件路径"""
    reports_dir = os.path.join(SCRIPT_DIR or '.', 'reports')
    os.makedirs(reports_dir, exist_ok=True)
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    return os.path.join(reports_dir, f'price_alert_{label}_{today}.txt')


def main():
    global SCRIPT_DIR
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

    parser = argparse.ArgumentParser(
        description='A股价格预警监控 v2.0（通达信协议/腾讯接口双源）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单只：中国能建跌破2.90提醒（默认用通达信郑州节点，~15ms）
  python price_alert.py --code sh601868 --target 2.90 --dir below

  # 每3秒检查一次（配合tdx源极速响应）
  python price_alert.py --code sh601868 --target 2.87 --dir below --interval 3

  # 强制使用腾讯接口
  python price_alert.py --code sh601868 --target 2.90 --dir below --source tencent

  # 指定其他通达信服务器
  python price_alert.py --code sh601868 --target 2.90 --dir below --host 123.125.108.103:7709

  # 双向：上海建工跌破2.45或涨破2.55都提醒
  python price_alert.py --code sh600170 --target 2.45 --dir both

  # 多只：用配置文件
  python price_alert.py --config my_alerts.json

JSON配置文件格式:
{
  "interval": 5,
  "source": "tdx",
  "log": "reports/my_alerts.txt",
  "alerts": [
    {"code": "sh601868", "name": "中国能建", "target": 2.90, "dir": "below"},
    {"code": "sz000560", "name": "我爱我家", "target": 2.65, "dir": "above"}
  ]
}
        """
    )

    parser.add_argument('--code', type=str, help='股票代码 (如 sh601868)')
    parser.add_argument('--name', type=str, default='', help='股票名称 (可选，自动获取)')
    parser.add_argument('--target', type=float, help='目标价格')
    parser.add_argument('--dir', type=str, default='below',
                        choices=['below', 'above', 'both'],
                        help='方向: below=跌破, above=涨破, both=双向')
    parser.add_argument('--interval', type=int, default=5, help='检查间隔秒数 (默认5)')
    parser.add_argument('--source', type=str, default='tdx',
                        choices=['tdx', 'tencent'],
                        help='数据源: tdx=通达信协议(默认,~15ms) | tencent=腾讯接口(~155ms)')
    parser.add_argument('--host', type=str, default='',
                        help='通达信服务器地址 (如 182.118.8.4:7709，默认郑州节点)')
    parser.add_argument('--log', type=str, default='', help='日志文件路径 (默认自动生成)')
    parser.add_argument('--config', type=str, help='JSON配置文件路径 (监控多只股票)')
    parser.add_argument('--notify', type=str, default='popup',
                        choices=['popup', 'log'],
                        help='提醒方式: popup=本地弹窗(默认) | log=仅写日志文件')

    args = parser.parse_args()

    if args.config:
        check_multi(args.config, args.source, args.host or None, args.notify)
    elif args.code and args.target:
        if not args.name:
            try:
                args.name = resolve_name(args.code, args.source, args.host or None)
            except Exception:
                args.name = args.code

        log_file = args.log or get_log_path(args.code)
        try:
            check_single(args.code, args.name, args.target, args.dir,
                           args.interval, log_file, args.source,
                           args.host or None, args.notify)
        finally:
            close_tdx_client()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
