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
# 接口失败跟踪器（连续5次无效返回 → 屏蔽至软件重启）
# ============================================================
SOURCE_FAIL_COUNTS = {}      # {source_name: int}  连续失败次数
SOURCE_DISABLED = set()      # {source_name}        被屏蔽的接口名
MAX_CONSECUTIVE_FAILS = 5    # 连续失败阈值

# 为什么要加锁：计数是「读-改-写」，非原子操作。get_quotes_batch 既会被 QuoteWorker
# 后台线程调用、也可能被 UI 线程（设置保存后）调用，并发下会丢计数，导致屏蔽判定失真。
# 锁内只做内存操作，print 放到锁外，避免持锁做 IO。
_SOURCE_LOCK = threading.Lock()

# A股多源轮询游标：原先藏在 get_quotes_batch 的函数属性里（getattr/setattr），
# 读-改-写非原子、并发调用会跳号。改为模块级变量 + 专用锁。
_a_share_cursor = 0
_a_share_cursor_lock = threading.Lock()

def _source_available(name):
    with _SOURCE_LOCK:
        return name not in SOURCE_DISABLED

def _source_mark_success(name):
    """接口返回有效数据 → 清零失败计数"""
    with _SOURCE_LOCK:
        SOURCE_FAIL_COUNTS[name] = 0

def _source_mark_fail(name, detail=''):
    """接口调用失败/无效 → 计数+1，达阈值则屏蔽"""
    with _SOURCE_LOCK:
        SOURCE_FAIL_COUNTS[name] = SOURCE_FAIL_COUNTS.get(name, 0) + 1
        cnt = SOURCE_FAIL_COUNTS[name]
    print(f'[SourceFail] {name}: 连续失败 {cnt}/{MAX_CONSECUTIVE_FAILS} {detail}')
    if cnt >= MAX_CONSECUTIVE_FAILS:
        with _SOURCE_LOCK:
            newly = name not in SOURCE_DISABLED
            if newly:
                SOURCE_DISABLED.add(name)
        if newly:
            print(f'[SourceDisabled] {name} 连续{MAX_CONSECUTIVE_FAILS}次失败，已屏蔽至重启')

def _batch_has_valid_result(results, codes):
    """判断批量结果是否有效：至少覆盖一半的股票，且价格与涨跌幅都齐全

    为什么要连 change_pct 一起查：只查 price>0 时，涨跌幅缺失的脏数据也会被判成
    「该源可用」，源不会被屏蔽、坏数据继续上屏，而涨跌幅预警恰恰依赖这个字段。
    """
    if not results:
        return False

    def _usable(rec):
        """单条记录能否用于预警：价格为正且涨跌幅存在"""
        try:
            price = rec.get('price')
            return price is not None and float(price) > 0 and rec.get('change_pct') is not None
        except (TypeError, ValueError):
            return False

    valid = sum(1 for c in codes if c in results and _usable(results[c]))
    return valid >= max(1, len(codes) // 2)


# ============================================================
# TDX节点配置（多节点故障转移）
# ============================================================

# 节点列表（按优先级排序，老板优选低延迟节点）
# 默认 TDX节点列表(可通过 watchlist_config.json 的 tdx_nodes 字段覆盖)
DEFAULT_TDX_NODES = [
    {'id': 'zhengzhou_z1', 'name': '云行情郑州主站Z1', 'host': '182.118.8.4:7709', 'priority': 1},
    {'id': 'shanghai_z1',  'name': '云行情上海主站Z1', 'host': '114.141.177.44:7709', 'priority': 2},
    {'id': 'beijing_z1',   'name': '云行情北京主站Z1', 'host': '123.125.108.103:7709', 'priority': 3},
]

# 运行时TDX节点列表(可被配置覆盖)
TDX_NODES = DEFAULT_TDX_NODES

# 默认节点(郑州主站,延迟最低)
DEFAULT_TDX_HOST = '182.118.8.4:7709'
DEFAULT_TDX_NODE_NAME = '云行情郑州主站Z1'

# 全局eltdx客户端（连接复用）
_tdx_client = None
_current_node = None
_failed_nodes = set()


def get_tdx_client(host=None, nodes=None):
    """获取或创建eltdx客户端(单例,连接复用,支持故障转移)"""
    global _tdx_client, _current_node, TDX_NODES

    if nodes is not None:
        TDX_NODES = nodes

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

    # 否则按优先级选择节点。
    # 失败过的节点不永久跳过、只排到队尾：原先直接 continue 会让「临时挂过、
    # 后来恢复」的节点在程序重启前永不可用；排到队尾既避开它的重试开销，
    # 又保留自动恢复能力。
    ordered = sorted(
        TDX_NODES,
        key=lambda n: (n['host'] in _failed_nodes, n.get('priority', 99)),
    )
    for node in ordered:
        try:
            _tdx_client = Client(host=node['host'])
            _current_node = node['host']
            _failed_nodes.discard(node['host'])   # 连上即撤销失败标记
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
    """从腾讯接口获取 A 股实时行情（备用，约 155ms 延迟）

    仅支持 A 股（sh/sz 前缀）；非 A 股代码直接抛错，
    避免上层拿到结构不符的脏数据。
    """
    import urllib.request
    if not code.lower().startswith(('sh', 'sz')):
        raise ValueError(f'腾讯接口仅支持 A 股代码(sh/sz): {code}')

    url = f'https://qt.gtimg.cn/q={code}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=5) as resp:
        content = resp.read().decode('gbk')

    f = content.split('~')
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

def detect_market(code):
    """识别是否为 A 股代码（含 s_ 前缀的指数写法）

    返回: 'a_share' | 'unknown'

    为什么保留：UI 与批量取数都要靠它区分「可监控的 A 股」与
    「不支持的代码」，是单一判定入口。
    """
    c = code.lower()
    if c.startswith(('sh', 'sz', 's_sh', 's_sz')):
        return 'a_share'
    return 'unknown'

def get_quote(code, source='auto', host=None):
    """统一行情获取接口（仅 A 股）

    source:
      auto    - A股优先通达信（低延迟），失败回退腾讯
      tdx     - 强制通达信协议
      tencent - 强制腾讯接口
    """
    # 市场校验必须前置于 source 分支：否则 source='tencent' 会绕过它直接进腾讯接口。
    # 腾讯那层虽然也会拦，但校验点分散在两处，将来改一处就容易漏掉另一处。
    if detect_market(code) != 'a_share':
        raise ValueError(f'仅支持 A 股代码（sh/sz 前缀），收到: {code}')

    if source == 'tencent':
        return get_quote_tencent(code)

    if source == 'tdx':
        return get_quote_tdx(code, host)

    # auto: A股优先 TDX（低延迟），失败回退腾讯
    try:
        return get_quote_tdx(code, host)
    except Exception:
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
    codes: A股代码列表，如 ['sh000001', 'sh601398']
    返回: {code: quote_dict, ...}，失败的代码不包含在结果中
    """
    import urllib.request
    import re

    # 项目已收敛为纯 A 股，代码原样透传
    # （这里不能过滤：下面 code_map 是按位索引 normalized 的，过滤会错位；
    #   真正的过滤在 get_quotes_batch 入口完成）
    normalized = list(codes)
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
        # A股标准格式
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
    """新浪接口批量获取 A 股行情（第二数据源）
    新浪需要 Referer 头，否则返回403。
    返回格式: var hq_str_sh601398="名称,今开,昨收,当前价,最高,最低,...";
    """
    import urllib.request
    import re as _re

    # 新浪仅取 A 股代码（sh/sz 及 s_ 指数写法）
    sina_codes = []
    for code in codes:
        c = code.lower()
        if c.startswith(('sh', 'sz', 's_sh', 's_sz')):
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
            # 盘前/集合竞价阶段 fields[3] 当前价=0，用买1价(fields[6])兜底
            if price is None or price <= 0:
                buy1 = float(fields[6]) if len(fields) > 6 and fields[6] else None
                if buy1 and buy1 > 0:
                    price = buy1
            # 再兜底：卖1价 fields[7]
            if price is None or price <= 0:
                sell1 = float(fields[7]) if len(fields) > 7 and fields[7] else None
                if sell1 and sell1 > 0:
                    price = sell1
            high = float(fields[4]) if fields[4] else None
            low = float(fields[5]) if fields[5] else None
            # high/low 兜底用 price
            if (high is None or high <= 0) and price and price > 0:
                high = price
            if (low is None or low <= 0) and price and price > 0:
                low = price
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


def get_quotes_tencent_sqt_batch(codes):
    """腾讯备用接口批量获取行情（第四数据源）- web.sqt.gtimg.cn
    独立域名，与 qt.gtimg.cn 分开算频率。返回格式完全一致。
    仅支持 A 股，解析逻辑与 get_quotes_tencent_batch 相同。
    """
    import urllib.request
    import re

    # 项目已收敛为纯 A 股，代码原样透传
    # （这里不能过滤：下面 code_map 是按位索引 normalized 的，过滤会错位；
    #   真正的过滤在 get_quotes_batch 入口完成）
    normalized = list(codes)
    codes_str = ','.join(normalized)
    url = f'https://web.sqt.gtimg.cn/q={codes_str}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            content = resp.read().decode('gbk')
    except Exception as e:
        print(f'[TencentSQT Batch] failed: {e}')
        return {}

    results = {}
    pattern = r'v_[^=]+="([^"]*)"'
    matches = re.findall(pattern, content)

    code_map = {}
    for i, code in enumerate(codes):
        code_map[code] = normalized[i]

    for match_str in matches:
        f = match_str.split('~')
        if len(f) < 5:
            continue
        ret_code = f[2] if len(f) > 2 else ''
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
        except (ValueError, IndexError):
            continue
        matched = False
        for orig_code, norm_code in code_map.items():
            if orig_code not in results:
                if norm_code == ret_code or norm_code.upper().endswith(ret_code.upper()):
                    if quote.get('price') is not None:
                        results[orig_code] = quote
                        matched = True
                        break
        if not matched and ret_code:
            for orig_code in codes:
                if orig_code not in results:
                    results[orig_code] = quote
                    break
    return results


def get_quotes_eastmoney_his_batch(codes):
    """东方财富历史接口批量获取A股行情（第五数据源）- push2his.eastmoney.com
    ulist批量端点字段含义错误（返回市值），改为循环调用 stock/get 单只端点。
    整数价存储，需 ÷100。secid 格式：1.code(沪) / 0.code(深)
    字段：f43=最新价*100  f44=最高*100  f45=最低*100  f46=今开*100  f58=名称  f60=昨收*100  f170=涨跌幅*100
    """
    import urllib.request
    import json as _json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    secid_map = {}  # secid -> orig_code
    for code in codes:
        c = code.lower()
        if c.startswith('sh'):
            secid_map[f'1.{c[2:]}'] = code
        elif c.startswith('sz'):
            secid_map[f'0.{c[2:]}'] = code
    if not secid_map:
        return {}

    ETF_PREFIXES = ('sh51', 'sz15', 'sz16', 'sh50', 'sh58')

    def _scale_for(code_lower):
        """ETF/基金价格需3位小数用×1000存储，其他股票2位小数用×100"""
        return 1000 if code_lower.startswith(ETF_PREFIXES) else 100

    def _f(v, scale):
        if v is None or v == '-' or v == '':
            return None
        try:
            return float(v) / scale
        except (ValueError, TypeError):
            return None

    def _fetch_one(secid):
        url = (
            f'http://push2his.eastmoney.com/api/qt/stock/get'
            f'?secid={secid}&fields=f43,f44,f45,f46,f58,f60,f170'
        )
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/'
        })
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = _json.loads(resp.read().decode('utf-8'))
            d = data.get('data') or {}
            if not d:
                return secid, None
            return secid, d
        except Exception:
            return secid, None

    results = {}
    secids = list(secid_map.keys())
    # 并发压到 3：原先 6 并发同时打同一域名，容易触发东财单 IP 限流
    with ThreadPoolExecutor(max_workers=min(3, len(secids))) as ex:
        for secid, d in ex.map(_fetch_one, secids):
            if not d:
                continue
            orig_code = secid_map[secid]
            scale = _scale_for(orig_code.lower())
            price = _f(d.get('f43'), scale)
            if price is None or price <= 0:
                continue
            results[orig_code] = {
                'name': d.get('f58', orig_code),
                'code': orig_code,
                'price': price,
                'yest_close': _f(d.get('f60'), scale),
                'open': _f(d.get('f46'), scale),
                'high': _f(d.get('f44'), scale),
                'low': _f(d.get('f45'), scale),
                'change_pct': _f(d.get('f170'), 100),  # 涨跌幅统一 ÷100
            }
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
        # 东方财富仅取 A 股（sh/sz）
    if not secids:
        return {}

    secids_str = ','.join(secids)
    url = (
        f'https://push2.eastmoney.com/api/qt/ulist.np/get'
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
        if price is None or price <= 0:
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
    """统一批量行情获取接口（仅 A 股）

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

    # 仅保留 A 股代码；非 A 股一律忽略（项目已收敛为纯 A 股监控）
    a_share_codes = [c for c in codes if detect_market(c) == 'a_share']
    if not a_share_codes:
        return {}

    results = {}

    # A股：五源轮询（腾讯主/新浪/东方财富行情/腾讯备用/东方财富历史），避免单一接口限流
    if a_share_codes:
        a_sources = ['tencent', 'sina', 'eastmoney', 'tencent_sqt', 'eastmoney_his']
        source_funcs = {
            'tencent':        get_quotes_tencent_batch,
            'sina':           get_quotes_sina_batch,
            'eastmoney':      get_quotes_eastmoney_batch,
            'tencent_sqt':    get_quotes_tencent_sqt_batch,
            'eastmoney_his':  get_quotes_eastmoney_his_batch,
        }
        # 过滤已屏蔽的接口，优先使用可用源
        available_sources = [s for s in a_sources if _source_available(s)]
        if not available_sources:
            # 极端情况：所有源都被屏蔽了，还是退化为全量轮询避免死锁
            available_sources = a_sources
        global _a_share_cursor
        with _a_share_cursor_lock:
            idx = _a_share_cursor
            _a_share_cursor = (idx + 1) % len(available_sources)
        cur_source = available_sources[idx % len(available_sources)]

        batch_results = {}
        func = source_funcs[cur_source]
        try:
            batch_results = func(a_share_codes)
        except Exception as e:
            print(f'[Source] {cur_source} 调用异常: {e}')
            batch_results = {}

        # 成功/失败判定
        if _batch_has_valid_result(batch_results, a_share_codes):
            _source_mark_success(cur_source)
        else:
            _source_mark_fail(cur_source, f'(结果: {len(batch_results)}/{len(a_share_codes)})')

        # 当前源获取不全的，优先用腾讯补充（腾讯作为兜底基准）
        missing = [c for c in a_share_codes
                   if c not in batch_results
                   or not batch_results[c].get('price')
                   or batch_results[c].get('price') <= 0]
        if missing:
            try:
                tencent_fill = get_quotes_tencent_batch(missing)
            except Exception:
                tencent_fill = {}
            for c in missing:
                if c in tencent_fill and tencent_fill[c].get('price') is not None:
                    batch_results[c] = tencent_fill[c]

        results.update(batch_results)

    return results


def get_kline(code, period='1min', count=60):
    """获取 A 股 K 线数据（多周期，带备用源）

    period: '1min' | 'daily' | 'weekly'
    数据源：东方财富 (klt=1/101/102) 优先，失败回退腾讯
    返回: [{'time','open','high','low','close','volume'}, ...]
    """
    if detect_market(code) != 'a_share':
        return []
    try:
        klines = _get_kline_eastmoney(code, count, period)
    except Exception as e:
        print(f'[KLine] eastmoney error: {e}')
        klines = []
    if not klines:
        # 东财是单点，它不可用时用腾讯兜底，避免 K 线悬浮图整体消失
        klines = _get_kline_tencent(code, count, period)
    return klines
def get_kline_1min(code, count=60):
    """获取1分钟K线数据（兼容旧接口）"""
    return get_kline(code, '1min', count)


def _get_kline_tencent(code, count=60, period='1min'):
    """腾讯 K 线（东方财富不可用时的备用源）

    为什么要有它：此前 K 线只有东方财富一条路，它一挂 K 线悬浮图就整体消失。
    接口返回字段顺序是「时间, 开, 收, 高, 低, 量」——注意是开收高低，
    不是常见的开高低收，映射时别搞错。
    """
    import json as _json
    import urllib.request

    kind = {'1min': 'm1', 'daily': 'day', 'weekly': 'week'}.get(period, 'm1')
    path = 'mkline' if kind == 'm1' else 'kline'
    param = f'{code},{kind},,{count}' if kind == 'm1' else f'{code},{kind},,,{count}'
    url = f'https://ifzq.gtimg.cn/appstock/app/kline/{path}?param={param}'

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = _json.loads(resp.read().decode('utf-8'))
        rows = (payload.get('data', {}).get(code, {}) or {}).get(kind) or []
    except Exception as e:
        print(f'[KLine Tencent] failed: {e}')
        return []

    out = []
    skipped = 0
    for r in rows:
        try:
            ts = str(r[0])
            # 分钟线时间形如 202609221104，日/周线形如 2026-09-22；
            # 认不出的格式直接跳过 —— 宁可少一根蜡烛，也不要塞脏数据进图
            if len(ts) >= 12 and ts.isdigit():
                t = f'{ts[8:10]}:{ts[10:12]}'
            elif len(ts) == 10 and ts[4] == '-':
                t = ts
            else:
                skipped += 1
                continue
            out.append({
                'time': t,
                'open': float(r[1]),
                'close': float(r[2]),
                'high': float(r[3]),
                'low': float(r[4]),
                'volume': float(r[5]) if r[5] else 0.0,
            })
        except (ValueError, IndexError):
            skipped += 1
            continue
    if skipped:
        print(f'[KLine Tencent] 跳过 {skipped} 条格式异常记录')
    return out


def _get_kline_eastmoney(code, count=60, period='1min'):
    """东方财富K线 (A股)
    period: '1min'(klt=1) | 'daily'(klt=101) | 'weekly'(klt=102)
    fqt=1 前复权
    """
    import urllib.request
    import json as _json

    klt_map = {'1min': 1, 'daily': 101, 'weekly': 102}
    klt = klt_map.get(period, 1)

    c = code.lower()
    if c.startswith('sh'):
        secid = f'1.{c[2:]}'
    elif c.startswith('sz'):
        secid = f'0.{c[2:]}'
    else:
        return []

    url = (
        f'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        f'?secid={secid}&klt={klt}&fqt=1&end=20500101&lmt={count}'
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
            raw_time = parts[0].split(' ')[0] if ' ' in parts[0] else parts[0]
            if period == '1min':
                time_str = parts[0].split(' ')[-1] if ' ' in parts[0] else parts[0]
            else:
                # 日K/周K: raw_time = 'YYYY-MM-DD' → 'MM-DD'
                segs = raw_time.split('-')
                time_str = f'{segs[1]}-{segs[2]}' if len(segs) >= 3 else raw_time
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
