#!/usr/bin/env python3
"""
price_alert.py — A股价格预警监控脚本 v5.5.0
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
import math
import os
import re
import sys
import time
import datetime
import subprocess
import threading
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SCRIPT_DIR = None

# A 股交易时区/节假日：不再依赖用户系统时区。Windows 通过 tzdata 提供 zoneinfo 数据。
try:
    CN_TZ = ZoneInfo('Asia/Shanghai')
except Exception:                       # 极端环境缺少 tzdata 时退化为固定 UTC+8
    CN_TZ = datetime.timezone(datetime.timedelta(hours=8), 'Asia/Shanghai')

# 静态交易日历（沪深交易所休市日；仅列工作日/调休休市，周末由 weekday 判断）。
# 维护方式：每年交易所公布下一年休市安排后，把新增日期补进这里即可。
# 2027 年日期为按农历/公休规律预排，交易所正式通知发布后应以官方口径复核。
CN_MARKET_HOLIDAYS = frozenset({
    # 2024
    '2024-01-01', '2024-02-09', '2024-02-12', '2024-02-13', '2024-02-14',
    '2024-02-15', '2024-02-16', '2024-04-04', '2024-04-05', '2024-05-01',
    '2024-05-02', '2024-05-03', '2024-06-10', '2024-09-16', '2024-09-17',
    '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-07',
    # 2025
    '2025-01-01', '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
    '2025-02-03', '2025-02-04', '2025-04-04', '2025-05-01', '2025-05-02',
    '2025-05-05', '2025-06-02', '2025-10-01', '2025-10-02', '2025-10-03',
    '2025-10-06', '2025-10-07', '2025-10-08',
    # 2026（国务院 2026 年部分节假日安排）
    '2026-01-01', '2026-01-02', '2026-02-16', '2026-02-17', '2026-02-18',
    '2026-02-19', '2026-02-20', '2026-02-23', '2026-04-06', '2026-05-01',
    '2026-05-04', '2026-05-05', '2026-06-19', '2026-09-25', '2026-10-01',
    '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07',
    # 2027（预排，待交易所正式通知复核）
    '2027-01-01', '2027-02-05', '2027-02-08', '2027-02-09', '2027-02-10',
    '2027-02-11', '2027-02-12', '2027-04-05', '2027-05-03', '2027-05-04',
    '2027-05-05', '2027-06-09', '2027-09-15', '2027-10-01', '2027-10-04',
    '2027-10-05', '2027-10-06', '2027-10-07',
})

# ============================================================
# HTTP 连接复用（模块级 opener）
# ============================================================
# 所有行情接口共用一个 OpenerDirector，避免每次请求重建连接、复用 TCP/TLS。
# 注意：项目无任何第三方依赖，严禁引入 requests。
_HTTP_OPENER = urllib.request.build_opener()


def _eastmoney_url_candidates(url):
    """东方财富 push2his 在部分网络下 HTTPS 握手会被重置，而同 URL 的 HTTP 可用。

    返回「HTTP 优先、HTTPS 兜底」的候选列表；非东财 push2his 地址原样返回。
    这是纯传输层兼容，不改接口路径和字段口径。
    """
    if 'push2his.eastmoney.com' not in url:
        return [url]
    if url.startswith('https://'):
        http_url = 'http://' + url[len('https://'):]
        return [http_url, url]
    if url.startswith('http://'):
        https_url = 'https://' + url[len('http://'):]
        return [url, https_url]
    return [url]


def _open_url_with_fallback(opener, req, timeout):
    """依次尝试候选 URL；返回 (response, url)。调用方负责 with 关闭 response。"""
    last_error = None
    for url in _eastmoney_url_candidates(req.full_url):
        candidate = urllib.request.Request(url, headers=dict(req.header_items()))
        try:
            return opener.open(candidate, timeout=timeout), url
        except Exception as exc:
            last_error = exc
    raise last_error or OSError('no candidate URL')

# A股场内基金/ETF/LOF/REIT 代码家族（行情一律 3 位小数）。
# 逐族对真实接口核过（腾讯 qt.gtimg.cn 的 f[3] 小数位）：
#   sh50 封基/LOF/REIT、sh51 ETF、sh52 跨境ETF(520xxx)、sh53 ETF(530000)、
#   sh55 科创债ETF(551xxx)、sh56 新ETF号段、sh58 科创ETF(588/589)、
#   sz15 ETF、sz16 LOF、sz18 REIT(180/184xxx)。
# sh54/sh57/sh59/sz17 当前无可交易代码，未列入；新号段上市后补这里即可。
ETF_PREFIXES = ('sh50', 'sh51', 'sh52', 'sh53', 'sh55', 'sh56', 'sh58',
                'sz15', 'sz16', 'sz18')

# A 股代码统一入口：大小写、s_ 指数前缀全部归一为小写 sh/sz + 6 位数字。
# 北交所（bj）暂不支持：项目现有行情源/解析口径都以沪深为准，与其让代码在
# 轮询里被静默过滤，不如在数据层和设置页都明确拒绝。
_A_SHARE_CODE_RE = re.compile(r'^(sh|sz)(\d{6})$')


def normalize_a_share_code(code):
    """把用户/配置里的沪深代码归一为 ``sh######`` / ``sz######``。

    支持：sh601398、SH601398、s_sh000001、S_SZ399001。
    不支持：北交所 bj######、港股 hk#####、裸 6 位数字（裸代码有沪深歧义，
    由设置页的解析流程补前缀）。无法识别时返回空串。
    """
    value = str(code or '').strip().lower()
    if value.startswith('s_'):
        value = value[2:]
    match = _A_SHARE_CODE_RE.fullmatch(value)
    return value if match else ''


def is_bj_code(code):
    """是否为北交所代码（当前版本明确拒绝，不再静默过滤）。"""
    value = str(code or '').strip().lower()
    return bool(re.fullmatch(r'bj\d{6}', value))


def require_a_share_code(code):
    """校验并返回规范 A 股代码；不支持时抛出带解释的 ValueError。"""
    normalized = normalize_a_share_code(code)
    if normalized:
        return normalized
    if is_bj_code(code):
        raise ValueError(f'暂不支持北交所代码: {code}（仅支持沪深 sh/sz）')
    raise ValueError(f'仅支持沪深 A 股代码（sh/sz + 6 位数字），收到: {code}')


def is_etf_code(code):
    """ETF/基金代码统一判定（数据层与界面格式化共用）。"""
    normalized = normalize_a_share_code(code) or str(code or '').strip().lower()
    return normalized.startswith(ETF_PREFIXES)


def price_decimals(code):
    """价格显示小数位：ETF/基金 3 位，其余 2 位。"""
    return 3 if is_etf_code(code) else 2


def format_price(code, price, prefix=''):
    """按代码类型统一格式化价格；None 显示 ``--``。"""
    if price is None:
        return '--'
    try:
        value = float(price)
    except (TypeError, ValueError):
        return '--'
    if not math.isfinite(value):
        return '--'
    return f'{prefix}{value:.{price_decimals(code)}f}'


def market_now(now=None):
    """返回 Asia/Shanghai 时区的当前时间。

    传入 naive datetime 时按「上海墙上时间」解释（测试/命令行常用），
    传入 aware datetime 时先转换到上海时区；也接受纯 date（按当天 00:00）。
    """
    if now is None:
        return datetime.datetime.now(CN_TZ)
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time.min)
    if now.tzinfo is None:
        return now.replace(tzinfo=CN_TZ)
    return now.astimezone(CN_TZ)


def is_a_share_trading_day(now=None):
    """是否为 A 股交易日：周一至周五且不在静态休市日列表。"""
    local = market_now(now)
    if local.weekday() >= 5:
        return False
    return local.date().isoformat() not in CN_MARKET_HOLIDAYS


QUOTE_MAX_AGE_SECONDS = 90


def _epoch_seconds(value):
    value = _finite_float(value)
    if value is None:
        return None
    if value > 100_000_000_000:
        value /= 1000
    return value


def _cn_timestamp(date_value, time_value):
    date_text = str(date_value or '').strip()
    time_text = str(time_value or '').strip()
    if date_text.endswith('.0'):
        date_text = date_text[:-2]
    if time_text.endswith('.0'):
        time_text = time_text[:-2]
    if len(time_text) in (4, 5) and time_text.isdigit():
        time_text = time_text.zfill(6)
    if len(date_text) == 8 and date_text.isdigit() and len(time_text) == 6 and time_text.isdigit():
        try:
            return datetime.datetime.strptime(
                date_text + time_text, '%Y%m%d%H%M%S'
            ).replace(tzinfo=CN_TZ).timestamp()
        except ValueError:
            return None
    return None


def _parse_cn_quote_time(value):
    text = str(value or '').strip()
    for fmt, length in (('%Y-%m-%d %H:%M:%S', 19), ('%Y-%m-%d %H:%M', 16)):
        try:
            return datetime.datetime.strptime(text[:length], fmt).replace(
                tzinfo=CN_TZ).timestamp()
        except ValueError:
            continue
    return None


def quote_is_stale(quote, now=None, max_age=QUOTE_MAX_AGE_SECONDS):
    """行情时间戳明显过期时为真；没有时间戳的源由调用方按市场时段判断。"""
    if not isinstance(quote, dict) or quote.get('stale'):
        return True
    timestamp = _epoch_seconds(quote.get('quote_ts'))
    if timestamp is None:
        return False
    now_ts = time.time() if now is None else now
    return now_ts - timestamp > max_age or timestamp - now_ts > 60


def is_a_share_trading_time(now=None):
    """判断当前是否在 A 股连续竞价/集合竞价时段（Asia/Shanghai）。

    交易时段：09:25-11:30、13:00-15:00；周末和中国法定休市日不交易。
    使用固定时区而非系统本地时间，海外/改过时区的机器也不会误判。
    """
    local = market_now(now)
    if not is_a_share_trading_day(local):
        return False
    current = local.time().replace(tzinfo=None)
    return (datetime.time(9, 25) <= current <= datetime.time(11, 30)
            or datetime.time(13, 0) <= current <= datetime.time(15, 0))

# ============================================================
# 接口失败跟踪器（连续 5 次无效返回 → 临时屏蔽 300 秒，到期自动解封）
# ============================================================
SOURCE_FAIL_COUNTS = {}      # {source_name: int}  连续失败次数
SOURCE_DISABLED = set()      # {source_name}        被屏蔽的接口名
SOURCE_DISABLED_TS = {}      # {source_name: float} 被屏蔽时的时间戳（用于时间衰减自动解封）
MAX_CONSECUTIVE_FAILS = 5    # 连续失败阈值
# 屏蔽时长(秒)：到期自动解封，避免「坏源永久不可用」直到重启。
# 这是 M3「时间衰减」的核心：屏蔽不是永久态，而是带 TTL 的临时态。
SOURCE_DISABLE_DURATION = 300

# 为什么要加锁：计数是「读-改-写」，非原子操作。get_quotes_batch 既会被 QuoteWorker
# 后台线程调用、也可能被 UI 线程（设置保存后）调用，并发下会丢计数，导致屏蔽判定失真。
# 锁内只做内存操作，print 放到锁外，避免持锁做 IO。
_SOURCE_LOCK = threading.Lock()

# A股多源轮询游标：原先藏在 get_quotes_batch 的函数属性里（getattr/setattr），
# 读-改-写非原子、并发调用会跳号。改为模块级变量 + 专用锁。
_a_share_cursor = 0
_a_share_cursor_lock = threading.Lock()

def _source_available(name):
    """接口是否可用：被屏蔽时检查是否过期，过期则自动解封（时间衰减）"""
    with _SOURCE_LOCK:
        if name not in SOURCE_DISABLED:
            return True
        ts = SOURCE_DISABLED_TS.get(name, 0)
        if time.time() - ts >= SOURCE_DISABLE_DURATION:
            # 屏蔽到期：自动解封并清零计数，恢复正常轮询
            SOURCE_DISABLED.discard(name)
            SOURCE_DISABLED_TS.pop(name, None)
            SOURCE_FAIL_COUNTS[name] = 0
            print(f'[SourceRecover] {name} 屏蔽到期({SOURCE_DISABLE_DURATION}s)，自动解封')
            return True
        return False

def _source_mark_success(name):
    """接口返回有效数据 → 清零失败计数，并立即解封（主动恢复）"""
    with _SOURCE_LOCK:
        was_disabled = name in SOURCE_DISABLED
        if was_disabled:
            SOURCE_DISABLED.discard(name)
            SOURCE_DISABLED_TS.pop(name, None)
            print(f'[SourceRecover] {name} 返回有效数据，立即解封')
        SOURCE_FAIL_COUNTS[name] = 0

def _source_mark_fail(name, detail=''):
    """接口调用失败/无效 → 计数+1，达阈值则屏蔽（记录屏蔽时间戳）

    计数递增、阈值判定、屏蔽写入必须在**同一个**临界区内完成：原先分两段加锁，
    中间窗口里并发的 _source_mark_success 可把计数清零并解封，本线程却仍按
    过期的 cnt 把它拉黑。print 留在锁外（持锁不做 IO）。
    """
    with _SOURCE_LOCK:
        SOURCE_FAIL_COUNTS[name] = SOURCE_FAIL_COUNTS.get(name, 0) + 1
        cnt = SOURCE_FAIL_COUNTS[name]
        newly = False
        if cnt >= MAX_CONSECUTIVE_FAILS and name not in SOURCE_DISABLED:
            SOURCE_DISABLED.add(name)
            SOURCE_DISABLED_TS[name] = time.time()
            newly = True
    print(f'[SourceFail] {name}: 连续失败 {cnt}/{MAX_CONSECUTIVE_FAILS} {detail}')
    if newly:
        print(f'[SourceDisabled] {name} 连续{MAX_CONSECUTIVE_FAILS}次失败，已屏蔽{SOURCE_DISABLE_DURATION}s')

def _source_recover(name):
    """手动/自动解封接口（时间衰减到期或主动恢复时调用）"""
    with _SOURCE_LOCK:
        SOURCE_DISABLED.discard(name)
        SOURCE_DISABLED_TS.pop(name, None)
        SOURCE_FAIL_COUNTS[name] = 0


A_SHARE_SOURCES = ('tencent', 'sina', 'eastmoney', 'tencent_sqt', 'eastmoney_his',
                   'wscn', '10jqka', 'sse')
SOURCE_DISPLAY_NAMES = {
    'tencent': '腾讯',
    'sina': '新浪',
    'eastmoney': '东方财富',
    'tencent_sqt': '腾讯备用',
    'eastmoney_his': '东财历史',
    'wscn': '华尔街见闻',
    '10jqka': '同花顺',
    'sse': '上交所官方',
}


def _source_coverable_codes(name, codes):
    """该源能覆盖的请求代码：上交所官方仅沪市，其余沪深都覆盖。"""
    if name == 'sse':
        return [c for c in codes if c.startswith('sh')]
    return list(codes)


def get_source_health():
    """返回数据源健康快照，供界面显示与日志诊断。

    结构：{source: {'available', 'fail_count', 'disabled', 'retry_in', 'label'}}
    本函数只读快照，不主动解封；TTL 到期后的解封发生在下一轮
    ``_source_available`` 判断，因此 ``retry_in`` 归零后最多再等一个轮询周期。
    """
    now = time.time()
    snapshot = {}
    with _SOURCE_LOCK:
        for name in A_SHARE_SOURCES:
            disabled = name in SOURCE_DISABLED
            disabled_ts = SOURCE_DISABLED_TS.get(name, 0)
            retry_in = 0
            if disabled:
                retry_in = max(0, int(SOURCE_DISABLE_DURATION - (now - disabled_ts)))
            snapshot[name] = {
                'label': SOURCE_DISPLAY_NAMES.get(name, name),
                'available': not disabled,
                'disabled': disabled,
                'fail_count': SOURCE_FAIL_COUNTS.get(name, 0),
                'retry_in': retry_in,
            }
    return snapshot


def _is_usable_quote(rec, require_change_pct=False):
    """单条行情能否上屏/参与源健康判定。

    价格必须为正的有限数；涨跌幅允许缺失（新股/停牌等显示 ``--``），但源健康
    判定要求它齐全，因为涨跌幅预警依赖该字段。
    """
    if not isinstance(rec, dict):
        return False
    try:
        price = float(rec.get('price'))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0:
        return False
    change_pct = rec.get('change_pct')
    if change_pct is not None:
        try:
            if not math.isfinite(float(change_pct)):
                return False
        except (TypeError, ValueError):
            return False
    if require_change_pct and change_pct is None:
        return False
    return True


def _filter_valid_quotes(results):
    """剔除价格无效/非有限数的记录，坏源数据不再污染界面与预警。"""
    if not isinstance(results, dict):
        return {}
    return {code: rec for code, rec in results.items() if _is_usable_quote(rec)}


def _batch_has_valid_result(results, codes):
    """判断批量结果是否有效：至少覆盖一半的股票，且价格与涨跌幅都齐全

    为什么要连 change_pct 一起查：只查 price>0 时，涨跌幅缺失的脏数据也会被判成
    「该源可用」，源不会被屏蔽、坏数据继续上屏，而涨跌幅预警恰恰依赖这个字段。
    """
    if not results:
        return False
    valid = sum(
        1 for c in codes
        if c in results and _is_usable_quote(results[c], require_change_pct=True)
    )
    return valid >= max(1, (len(codes) + 1) // 2)


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
    with _tdx_lock:
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


def configure_tdx_nodes(host='', nodes=None):
    """应用配置里的 TDX 节点；host 为空时恢复内置默认节点。

    为什么要有这个函数：旧实现只在 tdx_host 非空时覆盖模块全局，用户清空
    自定义节点后仍会继续连旧地址，直到重启。这里统一负责「覆盖 / 复位 / 断旧连接」。
    """
    global TDX_NODES, DEFAULT_TDX_HOST
    host = (host or '').strip()
    close_tdx_client()
    _failed_nodes.clear()
    if host:
        TDX_NODES = [{
            'id': 'custom', 'name': f'自定义({host})', 'host': host, 'priority': 1,
        }]
        DEFAULT_TDX_HOST = host
        return
    if nodes:
        TDX_NODES = nodes
        DEFAULT_TDX_HOST = nodes[0]['host']
        return
    TDX_NODES = [dict(node) for node in DEFAULT_TDX_NODES]
    DEFAULT_TDX_HOST = DEFAULT_TDX_NODES[0]['host']


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
                'Add-Type -AssemblyName System.Windows.Forms; '
                '[System.Windows.Forms.MessageBox]::Show('
                '$env:STOCK_MONITOR_MESSAGE, $env:STOCK_MONITOR_TITLE, '
                '[System.Windows.Forms.MessageBoxButtons]::OK, '
                '[System.Windows.Forms.MessageBoxIcon]::Warning)'
            )
            env = os.environ.copy()
            env['STOCK_MONITOR_MESSAGE'] = message
            env['STOCK_MONITOR_TITLE'] = title
            subprocess.Popen(
                ['powershell', '-NoProfile', '-Command', ps_cmd],
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=env,
            )
        except Exception:
            print(f'[POPUP FAILED] {title}: {message}')


def show_popup_async(title, message):
    """异步弹窗（不阻塞监控循环）"""
    t = threading.Thread(target=show_popup, args=(title, message), daemon=True)
    t.start()


# 必须是**可重入锁（RLock）**：get_quote_tdx 持锁后调用 get_tdx_client，
# 后者在切换节点/首次带 host 时会调用 close_tdx_client（其内部 again `with _tdx_lock`）。
# 若用普通 Lock，同一线程二次进入会自死锁、永久卡死取数线程。见 tests。
_tdx_lock = threading.RLock()


def get_quote_tdx(code, host=None):
    """从通达信协议获取实时行情（郑州节点，~15ms延迟）"""
    canonical = require_a_share_code(code)
    with _tdx_lock:
        client = get_tdx_client(host)
        result = client.get_quote(canonical)
    if not result or len(result) == 0:
        raise ValueError(f'未获取到数据: {canonical}')

    q = result[0]
    # 计算涨跌幅
    pre_close = q.pre_close_price
    last_price = q.last_price
    change_pct = None
    # last_price 可能为 None（停牌/盘前部分 TDX 节点），pre_close 有值时会算出
    # None - float 触发 TypeError。缺价就不算涨跌幅，交由上层显示 '--'。
    if pre_close and pre_close > 0 and last_price is not None:
        change_pct = ((last_price - pre_close) / pre_close) * 100

    return {
        'name': canonical,  # tdx协议不返回名称，用code
        'code': canonical,
        'price': last_price,
        'yest_close': pre_close,
        'open': q.open_price,
        'high': q.high_price,
        'low': q.low_price,
        'volume': q.total_hand,
        'amount': q.amount,
        'change_pct': change_pct,
        'quote_ts': _cn_timestamp(
            market_now().strftime('%Y%m%d'),
            getattr(q, 'time_raw', getattr(q, 'server_time_raw', '')),
        ),
        # 盘口
        'bid1': q.buy_levels[0].price if q.buy_levels else None,
        'bid1_vol': q.buy_levels[0].volume if q.buy_levels else None,
        'ask1': q.sell_levels[0].price if q.sell_levels else None,
        'ask1_vol': q.sell_levels[0].volume if q.sell_levels else None,
    }


# ============================================================
# 腾讯行情字段下标（统一命名常量，避免魔法数字散落三处解析逻辑）
# ============================================================
_TENCENT_F_NAME = 1
_TENCENT_F_CODE = 2
_TENCENT_F_PRICE = 3
_TENCENT_F_YEST_CLOSE = 4
_TENCENT_F_OPEN = 5
_TENCENT_F_CHANGE_PCT = 32
_TENCENT_F_HIGH = 33
_TENCENT_F_LOW = 34
_TENCENT_F_DATE = 30
_TENCENT_F_TIME = 31


def _parse_tencent_response(text):
    """解析单条腾讯行情内部串（v_xxx="..." 引号内的 ~ 分隔内容）。

    返回 quote dict；字段残缺（<5 段）或解析失败返回 None，由调用方决定跳过/报错。
    单只 / 批量 / sqt 三处腾讯解析统一走这里，避免重复且易错的下标逻辑。
    """
    f = text.split('~')
    if len(f) < 5:          # L9 防御：字段太少直接判残缺，杜绝 IndexError
        return None
    try:
        return {
            'name': f[_TENCENT_F_NAME],
            'code': f[_TENCENT_F_CODE],
            'price': float(f[_TENCENT_F_PRICE]) if f[_TENCENT_F_PRICE] else None,
            'yest_close': float(f[_TENCENT_F_YEST_CLOSE]) if f[_TENCENT_F_YEST_CLOSE] else None,
            'open': float(f[_TENCENT_F_OPEN]) if len(f) > _TENCENT_F_OPEN and f[_TENCENT_F_OPEN] else None,
            'high': float(f[_TENCENT_F_HIGH]) if len(f) > _TENCENT_F_HIGH and f[_TENCENT_F_HIGH] else None,
            'low': float(f[_TENCENT_F_LOW]) if len(f) > _TENCENT_F_LOW and f[_TENCENT_F_LOW] else None,
            'change_pct': float(f[_TENCENT_F_CHANGE_PCT]) if len(f) > _TENCENT_F_CHANGE_PCT and f[_TENCENT_F_CHANGE_PCT] else None,
            'quote_ts': _cn_timestamp(
                f[_TENCENT_F_DATE] if len(f) > _TENCENT_F_DATE else '',
                f[_TENCENT_F_TIME] if len(f) > _TENCENT_F_TIME else '',
            ),
        }
    except (ValueError, IndexError):
        return None


def get_quote_tencent(code):
    """从腾讯接口获取 A 股实时行情（备用，约 155ms 延迟）

    大小写、``s_`` 指数写法统一归一后请求；北交所/非沪深代码直接抛错，
    避免上层拿到结构不符的脏数据。
    """
    canonical = require_a_share_code(code)

    url = f'https://qt.gtimg.cn/q={canonical}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with _HTTP_OPENER.open(req, timeout=5) as resp:
        content = resp.read().decode('gbk')

    # 单只返回形如 v_code="字段0~字段1~..."; 用与批量一致的正则提取内部串，
    # 再走统一解析器。L9 畸形防御由 _parse_tencent_response 的残缺判定承担。
    m = re.search(r'v_[^=]+="([^"]*)"', content)
    if not m:
        raise ValueError(f'腾讯返回格式异常，无法匹配行情: {canonical}')
    quote = _parse_tencent_response(m.group(1))
    # 残缺或无价格 → 抛清晰异常，避免上层拿到脏数据
    if quote is None or not _is_usable_quote(quote):
        raise ValueError(f'腾讯返回字段残缺或价格无效: {canonical}')
    quote['code'] = canonical
    return quote


def search_stocks_by_name(query):
    """按名称/拼音模糊搜索沪深股票（腾讯 smartbox），返回 [(code, name), ...]。

    只保留 sh/sz 候选：北交所/港股本版本不支持，美股名称反查走 Alpaca assets。
    网络/格式异常向上抛，由调用方降级。
    """
    q = str(query or '').strip()
    if not q:
        return []
    url = 'https://smartbox.gtimg.cn/s3/?v=2&t=all&q=' + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with _HTTP_OPENER.open(req, timeout=5) as resp:
        content = resp.read().decode('gbk', errors='replace')
    m = re.search(r'v_hint="([^"]*)"', content)
    if not m:
        return []
    candidates = []
    for entry in m.group(1).split('^'):
        fields = entry.split('~')
        if len(fields) < 3:
            continue
        market, num, name = fields[0], fields[1], fields[2]
        if market not in ('sh', 'sz') or not re.fullmatch(r'\d{6}', num):
            continue
        # v=2 接口把中文写成 \uXXXX 字面量，这里还原成真实名称
        name = re.sub(r'\\u([0-9a-fA-F]{4})',
                      lambda mm: chr(int(mm.group(1), 16)), name)
        candidates.append((market + num, name))
    return candidates


def detect_market(code):
    """识别是否为 A 股代码（含 s_ 前缀的指数写法）

    返回: 'a_share' | 'unknown'

    为什么保留：UI 与批量取数都要靠它区分「可监控的 A 股」与
    「不支持的代码」，是单一判定入口。
    """
    return 'a_share' if normalize_a_share_code(code) else 'unknown'

def get_quote(code, source='auto', host=None):
    """统一行情获取接口（仅 A 股）

    source:
      auto    - A股优先通达信（低延迟），失败回退腾讯
      tdx     - 强制通达信协议
      tencent - 强制腾讯接口
    """
    # 市场校验必须前置于 source 分支：否则 source='tencent' 会绕过它直接进腾讯接口。
    # 腾讯那层虽然也会拦，但校验点分散在两处，将来改一处就容易漏掉另一处。
    code = require_a_share_code(code)

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




def _parse_tencent_batch(content, requested):
    """解析腾讯批量响应，并严格映射到请求代码。

    真实接口里 ``f[2]`` 通常只有 6 位数字（如 ``601398``），而外层变量名
    ``v_sh601398`` 才带市场前缀；同时 ``sh000001`` 与 ``sz000001`` 的 6 位数字
    完全相同。旧实现用 ``endswith`` + 盲兜底，空/缺失响应会整条错挂到别的股票。
    这里按「变量名 → 完整代码 → 唯一 6 位代码」逐级匹配，任何歧义都不写入。
    """
    requested = list(dict.fromkeys(requested))
    requested_set = set(requested)
    by_digits = {}
    for code in requested:
        by_digits.setdefault(code[2:], []).append(code)

    def _resolve_one(quote, var_code):
        var_norm = normalize_a_share_code(var_code)
        raw_code = str(quote.get('code') or '').strip()
        raw_norm = normalize_a_share_code(raw_code)

        # 外层变量名 / f[2] 只要能识别出完整市场前缀，就必须与请求集合一致：
        # 请求 sh000001 却收到 v_sz000001 时绝不能靠“唯一 6 位数字”错挂过去。
        # 两者都带前缀却不一致时，说明响应自相矛盾，宁可丢弃也不猜。
        if var_norm and raw_norm and var_norm != raw_norm:
            return None
        if var_norm:
            return var_norm if var_norm in requested_set else None
        if raw_norm:
            return raw_norm if raw_norm in requested_set else None

        digits = None
        if re.fullmatch(r'\d{6}', raw_code):
            digits = raw_code
        elif re.fullmatch(r'\d{6}', var_code.strip()):
            digits = var_code.strip()
        if digits:
            candidates = by_digits.get(digits, [])
            if len(candidates) == 1:
                return candidates[0]
        return None

    results = {}
    for var_code, inner in re.findall(r'v_([^=]+)="([^"]*)"', content):
        quote = _parse_tencent_response(inner)
        if quote is None or not _is_usable_quote(quote):
            continue
        target = _resolve_one(quote, var_code)
        if target and target not in results:
            quote['code'] = target
            results[target] = quote
    return results


def get_quotes_tencent_batch(codes):
    """腾讯接口批量获取行情（一次请求多只股票）。

    入参代码会归一为小写 sh/sz；返回键同为规范代码。失败的代码不包含在结果中。
    """
    normalized = []
    for code in codes:
        canonical = normalize_a_share_code(code)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        return {}

    codes_str = ','.join(normalized)
    url = f'https://qt.gtimg.cn/q={codes_str}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with _HTTP_OPENER.open(req, timeout=3) as resp:
            content = resp.read().decode('gbk')
    except Exception as e:
        print(f'[Tencent Batch] failed: {e}')
        return {}

    return _parse_tencent_batch(content, normalized)


def get_quotes_sina_batch(codes):
    """新浪接口批量获取 A 股行情（第二数据源）
    新浪需要 Referer 头，否则返回403。
    返回格式: var hq_str_sh601398="名称,今开,昨收,当前价,最高,最低,...";
    """
    # 新浪仅取规范沪深代码；s_ 前缀统一剥掉（新浪全量行情用标准代码即可）。
    sina_codes = []
    for code in codes:
        canonical = normalize_a_share_code(code)
        if canonical and canonical not in sina_codes:
            sina_codes.append(canonical)
    if not sina_codes:
        return {}

    requested = set(sina_codes)
    codes_str = ','.join(sina_codes)
    url = f'https://hq.sinajs.cn/list={codes_str}'
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://finance.sina.com.cn'
    })
    try:
        with _HTTP_OPENER.open(req, timeout=3) as resp:
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
        code_part = normalize_a_share_code(prefix.replace('var hq_str_', '').strip())
        if not code_part or code_part not in requested:
            continue
        m = re.search(r'"([^"]*)"', value)
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
                'quote_ts': _cn_timestamp(
                    fields[30] if len(fields) > 30 else '',
                    fields[31] if len(fields) > 31 else '',
                ),
            }
        except (ValueError, IndexError):
            continue
    return results


def get_quotes_tencent_sqt_batch(codes):
    """腾讯备用接口批量获取行情（第四数据源）- web.sqt.gtimg.cn
    独立域名，与 qt.gtimg.cn 分开算频率。返回格式完全一致。
    仅支持 A 股，解析/映射逻辑与 get_quotes_tencent_batch 共用。
    """
    normalized = []
    for code in codes:
        canonical = normalize_a_share_code(code)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        return {}

    codes_str = ','.join(normalized)
    url = f'https://web.sqt.gtimg.cn/q={codes_str}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with _HTTP_OPENER.open(req, timeout=3) as resp:
            content = resp.read().decode('gbk')
    except Exception as e:
        print(f'[TencentSQT Batch] failed: {e}')
        return {}

    return _parse_tencent_batch(content, normalized)


def get_quotes_eastmoney_his_batch(codes):
    """东方财富历史接口批量获取A股行情（第五数据源）- push2his.eastmoney.com
    ulist批量端点字段含义错误（返回市值），改为循环调用 stock/get 单只端点。
    整数价存储，需 ÷100。secid 格式：1.code(沪) / 0.code(深)
    字段：f43=最新价*100  f44=最高*100  f45=最低*100  f46=今开*100  f58=名称  f60=昨收*100  f170=涨跌幅*100
    """
    import urllib.request
    import json as _json
    from concurrent.futures import ThreadPoolExecutor

    secid_map = {}  # secid -> canonical code
    for code in codes:
        canonical = normalize_a_share_code(code)
        if not canonical:
            continue
        if canonical.startswith('sh'):
            secid_map[f'1.{canonical[2:]}'] = canonical
        else:
            secid_map[f'0.{canonical[2:]}'] = canonical
    if not secid_map:
        return {}

    def _scale_for(code_lower):
        """ETF/基金价格需3位小数用×1000存储，其他股票2位小数用×100"""
        return 1000 if is_etf_code(code_lower) else 100

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
            f'?secid={secid}&fields=f43,f44,f45,f46,f58,f60,f170,f124'
        )
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://quote.eastmoney.com/'
        })
        try:
            resp, _used_url = _open_url_with_fallback(_HTTP_OPENER, req, 3)
            with resp:
                data = _json.loads(resp.read().decode('utf-8'))
            if not isinstance(data, dict):
                return secid, None
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
                'quote_ts': _epoch_seconds(d.get('f124')),
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
        canonical = normalize_a_share_code(code)
        if not canonical:
            continue
        secid = f'1.{canonical[2:]}' if canonical.startswith('sh') else f'0.{canonical[2:]}'
        if secid not in secids:
            secids.append(secid)
        # 东方财富仅取 A 股（sh/sz）
    if not secids:
        return {}

    secids_str = ','.join(secids)
    url = (
        f'https://push2.eastmoney.com/api/qt/ulist.np/get'
        f'?fltt=2&fields=f2,f3,f12,f13,f14,f15,f16,f17,f18,f124'
        f'&secids={secids_str}'
    )
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://quote.eastmoney.com/'
    })
    try:
        with _HTTP_OPENER.open(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[EastMoney Batch] failed: {e}')
        return {}

    # 东财坏请求时常返回 {"data": null}——.get('data', {}) 会拿到 None 再 .get 抛
    # AttributeError（此行在上面的 try 之外）。用 `or {}` + 顶层类型守卫。
    if not isinstance(data, dict):
        return {}
    diff = (data.get('data') or {}).get('diff', [])
    if not isinstance(diff, list) or not diff:
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
        if not isinstance(item, dict):
            continue
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
            'quote_ts': _epoch_seconds(item.get('f124')),
        }
    return results


def _finite_float(val):
    """只接受有限数的浮点转换；坏值（None/'-'/空串/NaN/inf）一律 None。"""
    if val is None or val == '-' or val == '':
        return None
    try:
        v = float(val)
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) else None


def get_quotes_wscn_batch(codes):
    """华尔街见闻批量行情（JSON，无鉴权，沪深都覆盖）。

    字段按 data.fields 名字对齐取值（接口会丢弃不支持的字段名，顺序不保证）；
    不提供昨收，由现价与涨跌幅反推（px_change_rate 单位是百分数）。
    """
    normalized = []
    for code in codes:
        canonical = normalize_a_share_code(code)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    if not normalized:
        return {}
    prod_by_code = {c[2:] + ('.SS' if c.startswith('sh') else '.SZ'): c
                    for c in normalized}
    url = ('https://api-ddc-wscn.awtmt.com/market/real?prod_code='
           + ','.join(prod_by_code)
           + '&fields=prod_name,last_px,open_px,high_px,low_px,px_change_rate,update_time')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with _HTTP_OPENER.open(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[WSCN Batch] failed: {e}')
        return {}
    payload = (data or {}).get('data') or {}
    fields = payload.get('fields') or []
    snapshot = payload.get('snapshot') or {}
    idx = {f: i for i, f in enumerate(fields)}
    if 'last_px' not in idx:
        return {}

    def _num(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return None
        return _finite_float(row[i])

    results = {}
    for prod, code in prod_by_code.items():
        row = snapshot.get(prod)
        if not isinstance(row, list):
            continue
        price = _num(row, 'last_px')
        if price is None or price <= 0:
            continue
        pct = _num(row, 'px_change_rate')
        yest = price / (1 + pct / 100) if pct is not None and pct > -100 else None
        high, low = _num(row, 'high_px'), _num(row, 'low_px')
        if (high is None or high <= 0) and price > 0:
            high = price
        if (low is None or low <= 0) and price > 0:
            low = price
        name_i = idx.get('prod_name')
        name = row[name_i] if name_i is not None and name_i < len(row) else code
        results[code] = {
            'name': name,
            'code': code,
            'price': price,
            'yest_close': yest,
            'open': _num(row, 'open_px'),
            'high': high,
            'low': low,
            'change_pct': pct,
            'quote_ts': _epoch_seconds(row[idx['update_time']]) if 'update_time' in idx and idx['update_time'] < len(row) else None,
        }
    return results


def _ths_realhead_symbol(canonical):
    """把规范 A 股代码转成同花顺 realhead 接口的 symbol（路径 ``hs_<symbol>``）。

    同花顺的指数与个股共用 ``hs_`` 路径，但指数另有内部编码，且会与个股撞号：
      - 上证指数 sh000001 → 1A0001（1A 系列专用于上证指数）
      - 其它沪市指数 sh000xxx → 1B + 后 4 位
        （沪深300 000300 → 1B0300、上证50 000016 → 1B0016、
         科创50 000688 → 1B0688，均已实测可用）
      - 深市指数 sz399xxx → 6 位数字本身（深证成指 399001、创业板指 399006）
      - 个股 → 6 位数字本身

    为什么必须单独映射：若把 sh000001 直接按 ``000001`` 请求，同花顺会返回
    平安银行（约 11 元），上证指数 3500 点就被错显示成 11.x（真实故障）。
    """
    digits = canonical[2:]
    if canonical.startswith('sh') and digits.startswith('000'):
        return '1A0001' if digits == '000001' else '1B' + digits[2:]
    return digits


def get_quote_10jqka(code):
    """同花顺 realhead 单票实时（数字键映射，无需 Referer）。

    键映射（盘中对腾讯基准核验）：6=昨收 7=今开 8=最高 9=最低 10=最新
    24=买1价 30=卖1价；name 实测在 items 内（部分节点/时段顶层也有，两处都认）。
    盘前最新价可能为 0，用买1/卖1兜底（与新浪同口径）。
    """
    canonical = require_a_share_code(code)
    symbol = _ths_realhead_symbol(canonical)
    url = f'http://d.10jqka.com.cn/v2/realhead/hs_{symbol}/last.js'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with _HTTP_OPENER.open(req, timeout=3) as resp:
        body = resp.read().decode('utf-8', errors='replace')
    start, end = body.find('('), body.rfind(')')
    if start < 0 or end <= start:
        raise ValueError(f'同花顺返回格式异常: {canonical}')
    obj = json.loads(body[start + 1:end]) or {}
    items = obj.get('items') or {}
    price = _finite_float(items.get('10'))
    if price is None or price <= 0:
        price = _finite_float(items.get('24'))
    if price is None or price <= 0:
        price = _finite_float(items.get('30'))
    yest = _finite_float(items.get('6'))
    high, low = _finite_float(items.get('8')), _finite_float(items.get('9'))
    if (high is None or high <= 0) and price and price > 0:
        high = price
    if (low is None or low <= 0) and price and price > 0:
        low = price
    change_pct = None
    if price is not None and yest and yest > 0:
        change_pct = (price - yest) / yest * 100
    return {
        'name': obj.get('name') or items.get('name') or canonical,
        'code': canonical,
        'price': price,
        'yest_close': yest,
        'open': _finite_float(items.get('7')),
        'high': high,
        'low': low,
        'change_pct': change_pct,
        'quote_ts': _parse_cn_quote_time(obj.get('time')),
    }


def get_quotes_10jqka_batch(codes):
    """同花顺批量适配器：单票端点循环（沪深都覆盖）。

    连续 2 只异常即停（节点大概率挂了），剩下的交给轮询层的腾讯补齐，
    避免 N 只 × 3s 超时把整轮轮询拖死。
    """
    results = {}
    consecutive_errors = 0
    for code in dict.fromkeys(normalize_a_share_code(c) for c in codes):
        if not code:
            continue
        try:
            quote = get_quote_10jqka(code)
            if _is_usable_quote(quote):
                results[code] = quote
            consecutive_errors = 0
        except Exception as e:
            print(f'[10jqka Batch] {code}: {e}')
            consecutive_errors += 1
            if consecutive_errors >= 2:
                break
    return results


def get_quote_sse(code):
    """上交所官方快照（yunhq，仅沪市，交易所一手源）。

    snap 顺序（盘中对腾讯基准核验）：0=名称 1=昨收 2=今开 3=最高 4=最低
    5=最新 6=涨跌额 7=涨跌幅 8=量 9=额，其后为五档盘口。
    """
    canonical = require_a_share_code(code)
    if not canonical.startswith('sh'):
        raise ValueError(f'上交所官方源仅支持沪市: {canonical}')
    url = f'http://yunhq.sse.com.cn:32041/v1/sh1/snap/{canonical[2:]}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with _HTTP_OPENER.open(req, timeout=3) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    snap = (data or {}).get('snap') or []
    if len(snap) < 6:
        raise ValueError(f'上交所返回字段残缺: {canonical}')
    price = _finite_float(snap[5])
    yest = _finite_float(snap[1])
    high, low = _finite_float(snap[3]), _finite_float(snap[4])
    if (high is None or high <= 0) and price and price > 0:
        high = price
    if (low is None or low <= 0) and price and price > 0:
        low = price
    change_pct = None
    if price is not None and yest and yest > 0:
        change_pct = (price - yest) / yest * 100
    return {
        'name': snap[0] or canonical,
        'code': canonical,
        'price': price,
        'yest_close': yest,
        'open': _finite_float(snap[2]),
        'high': high,
        'low': low,
        'change_pct': change_pct,
        'quote_ts': _cn_timestamp(data.get('date'), data.get('time')),
    }


def get_quotes_sse_batch(codes):
    """上交所官方批量适配器：只取沪市代码。

    深市天然不覆盖——轮询层按覆盖范围把本排除（或只判沪市子集），
    不把「覆盖不到」当成「查询失败」去累计熔断计数。
    """
    results = {}
    consecutive_errors = 0
    for code in dict.fromkeys(normalize_a_share_code(c) for c in codes):
        if not code or not code.startswith('sh'):
            continue
        try:
            quote = get_quote_sse(code)
            if _is_usable_quote(quote):
                results[code] = quote
            consecutive_errors = 0
        except Exception as e:
            print(f'[SSE Batch] {code}: {e}')
            consecutive_errors += 1
            if consecutive_errors >= 2:
                break
    return results


def _map_results_to_input(results, original_by_canonical):
    """把数据层规范代码键映射回用户配置里的原始写法（如 SH601398）。"""
    mapped = {}
    for canonical, quote in results.items():
        for original in original_by_canonical.get(canonical, ()):
            mapped[original] = quote
    return mapped


def get_quotes_batch(codes, source='auto', host=None):
    """统一批量行情获取接口（仅 A 股）。

    返回键保持入参写法（配置里写 ``SH601398`` 也能正确回填卡片），
    值中的 ``code`` 统一为规范小写代码。
    """
    if not codes:
        return {}

    original_by_canonical = {}
    canonical_codes = []
    for code in codes:
        canonical = normalize_a_share_code(code)
        if not canonical:
            continue
        original_by_canonical.setdefault(canonical, []).append(code)
        if canonical not in canonical_codes:
            canonical_codes.append(canonical)

    # 非 A 股一律忽略（美股由 alpaca_stream/alpaca_history 单独取数）
    if not canonical_codes:
        return {}

    # 强制指定数据源
    if source == 'tencent':
        results = _filter_valid_quotes(get_quotes_tencent_batch(canonical_codes))
        return _map_results_to_input(results, original_by_canonical)

    if source == 'tdx':
        results = {}
        for code in canonical_codes:
            try:
                quote = get_quote_tdx(code, host)
                if _is_usable_quote(quote):
                    results[code] = quote
            except Exception as e:
                print(f'[TDX Batch] Error for {code}: {e}')
        return _map_results_to_input(results, original_by_canonical)

    # A股：八源轮询（腾讯/新浪/东财行情/腾讯备用/东财历史/华尔街见闻/同花顺/上交所官方），
    # 避免单一接口限流；连续 5 次失败的源屏蔽 300 秒（见 _source_mark_fail）
    source_funcs = {
        'tencent':        get_quotes_tencent_batch,
        'sina':           get_quotes_sina_batch,
        'eastmoney':      get_quotes_eastmoney_batch,
        'tencent_sqt':    get_quotes_tencent_sqt_batch,
        'eastmoney_his':  get_quotes_eastmoney_his_batch,
        'wscn':           get_quotes_wscn_batch,
        '10jqka':         get_quotes_10jqka_batch,
        'sse':            get_quotes_sse_batch,
    }
    # 过滤已屏蔽的接口；覆盖不到任何请求代码的源（如纯深市列表遇上上交所官方）
    # 本轮直接排除，不算失败
    available_sources = [s for s in A_SHARE_SOURCES
                         if _source_available(s) and _source_coverable_codes(s, canonical_codes)]
    if not available_sources:
        # 全源冷却中时保持冷却，不能每轮立即重试，否则 300 秒屏蔽形同虚设。
        return {}
    global _a_share_cursor
    with _a_share_cursor_lock:
        idx = _a_share_cursor
        _a_share_cursor = (idx + 1) % len(available_sources)
    cur_source = available_sources[idx % len(available_sources)]

    coverage = _source_coverable_codes(cur_source, canonical_codes)

    batch_results = {}
    func = source_funcs[cur_source]
    try:
        batch_results = func(coverage)
    except Exception as e:
        print(f'[Source] {cur_source} 调用异常: {e}')
        batch_results = {}

    # 坏数据（None/0/负价/NaN）不参与源健康判定，也不允许上屏。
    batch_results = _filter_valid_quotes(batch_results)
    # 健康判定只对该源能覆盖的子集：上交所官方拿不到深市不是它的失败
    if _batch_has_valid_result(batch_results, coverage):
        _source_mark_success(cur_source)
    else:
        _source_mark_fail(cur_source, f'(结果: {len(batch_results)}/{len(coverage)})')

    # 当前源缺记录或缺涨跌幅时都用备用腾讯源补齐；百分比预警不能静默跳过。
    missing = [c for c in canonical_codes
               if c not in batch_results
               or not _is_usable_quote(batch_results[c], require_change_pct=True)]
    fallback_source = 'tencent_sqt' if cur_source == 'tencent' else 'tencent'
    if missing and _source_available(fallback_source):
        try:
            fallback_fill = _filter_valid_quotes(source_funcs[fallback_source](missing))
        except Exception:
            fallback_fill = {}
        if _batch_has_valid_result(fallback_fill, missing):
            _source_mark_success(fallback_source)
        else:
            _source_mark_fail(
                fallback_source, f'(兜底结果: {len(fallback_fill)}/{len(missing)})')
        for c in missing:
            if c in fallback_fill:
                batch_results[c] = fallback_fill[c]

    return _map_results_to_input(batch_results, original_by_canonical)


def get_kline(code, period='1min', count=60, latest_day_only=False):
    """获取 A 股 K 线数据（多周期，带备用源）

    period: '1min' | 'daily' | 'weekly'
    数据源：东方财富 (klt=1/101/102) 优先，失败回退腾讯
    latest_day_only: 仅保留最后一根K线所属交易日的数据。分时全天化用：
                     先拉 count=300 根（覆盖全天240根+缓冲），再按最后一根
                     的日期过滤，得到"当天开盘至当前"的全部数据，避免跨天混杂
    返回: [{'time','open','high','low','close','volume'}, ...]
    """
    canonical = normalize_a_share_code(code)
    if not canonical:
        return []
    code = canonical
    try:
        klines = _get_kline_eastmoney(code, count, period, latest_day_only)
    except Exception as e:
        print(f'[KLine] eastmoney error: {e}')
        klines = []
    if not klines:
        # 东财是单点，它不可用时用腾讯兜底，避免 K 线悬浮图整体消失
        klines = _get_kline_tencent(code, count, period, latest_day_only)
    return klines
def get_kline_1min(code, count=60, latest_day_only=False):
    """获取1分钟K线数据（兼容旧接口）"""
    return get_kline(code, '1min', count, latest_day_only)


def _get_kline_tencent(code, count=60, period='1min', latest_day_only=False):
    """腾讯 K 线（东方财富不可用时的备用源）

    为什么要有它：此前 K 线只有东方财富一条路，它一挂 K 线悬浮图就整体消失。
    接口返回字段顺序是「时间, 开, 收, 高, 低, 量」——注意是开收高低，
    不是常见的开高低收，映射时别搞错。
    latest_day_only: 仅保留最后一根K线所属交易日的数据（分时全天化）
    """
    import json as _json

    code = normalize_a_share_code(code)
    if not code:
        return []
    kind = {'1min': 'm1', 'daily': 'day', 'weekly': 'week'}.get(period, 'm1')
    path = 'mkline' if kind == 'm1' else 'kline'
    param = f'{code},{kind},,{count}' if kind == 'm1' else f'{code},{kind},,,{count}'
    url = f'https://ifzq.gtimg.cn/appstock/app/kline/{path}?param={param}'

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with _HTTP_OPENER.open(req, timeout=5) as resp:
            payload = _json.loads(resp.read().decode('utf-8'))
        rows = (payload.get('data', {}).get(code, {}) or {}).get(kind) or []
    except Exception as e:
        print(f'[KLine Tencent] failed: {e}')
        return []
    if not isinstance(rows, list):
        print(f'[KLine Tencent] 返回结构异常: {type(rows).__name__}')
        return []

    out = []
    days = []   # 与 out 一一对应的原始日期（分钟线 yyyymmdd / 日周K yyyy-mm-dd），供按日过滤
    skipped = 0
    for r in rows:
        try:
            ts = str(r[0])
            # 分钟线时间形如 202609221104，日/周线形如 2026-09-22；
            # 认不出的格式直接跳过 —— 宁可少一根蜡烛，也不要塞脏数据进图
            if len(ts) >= 12 and ts.isdigit():
                t = f'{ts[8:10]}:{ts[10:12]}'
                day = ts[:8]
            elif len(ts) == 10 and ts[4] == '-':
                t = ts
                day = ts[:10]
            else:
                skipped += 1
                continue
            # 日期必须跟着成功解析的蜡烛一起入列：若先 append days、后因
            # 价格字段坏掉跳过 out，下面 zip(out, days) 会整体错位，
            # latest_day_only 就会按错误的日期过滤。
            candle = {
                'time': t,
                'open': float(r[1]),
                'close': float(r[2]),
                'high': float(r[3]),
                'low': float(r[4]),
                'volume': float(r[5]) if r[5] else 0.0,
            }
            out.append(candle)
            days.append(day)
        except (ValueError, TypeError, IndexError):
            skipped += 1
            continue
    if skipped:
        print(f'[KLine Tencent] 跳过 {skipped} 条格式异常记录')
    if latest_day_only and out:
        # 按最后一根的日期过滤：只保留同一交易日的数据（分时全天化）
        last_day = days[-1]
        out = [k for k, d in zip(out, days) if d == last_day]
    return out


def _get_kline_eastmoney(code, count=60, period='1min', latest_day_only=False):
    """东方财富K线 (A股)
    period: '1min'(klt=1) | 'daily'(klt=101) | 'weekly'(klt=102)
    fqt=1 前复权
    latest_day_only: 仅保留最后一根K线所属交易日的数据（分时全天化）
    """
    import urllib.request
    import json as _json

    klt_map = {'1min': 1, 'daily': 101, 'weekly': 102}
    klt = klt_map.get(period, 1)

    canonical = normalize_a_share_code(code)
    if not canonical:
        return []
    if canonical.startswith('sh'):
        secid = f'1.{canonical[2:]}'
    else:
        secid = f'0.{canonical[2:]}'

    url = (
        f'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        f'?secid={secid}&klt={klt}&fqt=1&end=20500101&lmt={count}'
        f'&fields1=f1,f2,f3,f5,f6'
        f'&fields2=f51,f52,f53,f54,f55,f56'
    )
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        # HTTPS 在部分网络会被远端直接断开；_open_url_with_fallback 先 HTTP 再 HTTPS
        resp, _used_url = _open_url_with_fallback(_HTTP_OPENER, req, 3)
        with resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f'[KLine EastMoney] failed: {e}')
        return []

    # 同东财批量：坏请求返回 {"data": null}，.get('data', {}) 得到 None 再 .get 抛
    # AttributeError。`or {}` + 顶层守卫兜住。
    if not isinstance(data, dict):
        return []
    klines = (data.get('data') or {}).get('klines', [])
    if not isinstance(klines, list) or not klines:
        return []

    result = []
    days = []   # 与 result 一一对应的原始日期（'YYYY-MM-DD'），供按日过滤
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
            days.append(raw_time)
        except (ValueError, TypeError, IndexError):
            continue
    if latest_day_only and result:
        # 按最后一根的日期过滤：只保留同一交易日的数据（分时全天化）
        last_day = days[-1]
        result = [k for k, d in zip(result, days) if d == last_day]
    return result


def check_single(code, name, target, direction, interval, log_file,
                 source='tdx', host=None, notify='popup'):
    """监控单只股票"""
    source_label = f'TDX({host or DEFAULT_TDX_HOST})' if source == 'tdx' else 'Tencent'
    print(f'========================================')
    print(f'  PRICE ALERT MONITOR v5.5.0')
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
    alerted = False

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
            if not is_a_share_trading_time() or quote_is_stale(q):
                alerted = False
                print(f'[{now}] 非交易时段或行情已过期，跳过预警')
                time.sleep(interval)
                continue
            check_count += 1

            triggered = False
            reasons = []

            if alert_below and price <= target:
                triggered = True
                reasons.append(f'price {format_price(code, price)} <= '
                               f'target {format_price(code, target)}')
            if alert_above and price >= target:
                triggered = True
                reasons.append(f'price {format_price(code, price)} >= '
                               f'target {format_price(code, target)}')

            if triggered:
                diff = price - target
                direction_cn = '跌破' if diff <= 0 else '涨破'
                pct_str = f'{q["change_pct"]:+.2f}%' if q.get('change_pct') is not None else 'N/A'
                if not alerted:
                    alerted = True
                    alert_line = (
                        f'[{now}] *** ALERT *** {name}({code}) '
                        f'{direction_cn} {format_price(code, target)} | '
                        f'当前价: {format_price(code, price)} | '
                        f'涨跌: {pct_str}'
                    )
                    print(alert_line)
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(alert_line + '\n')
                    # 弹窗通知
                    if notify == 'popup':
                        popup_msg = (
                            f'{name}({code}) {direction_cn} {format_price(code, target)}\n'
                            f'当前价: {format_price(code, price)}\n'
                            f'涨跌幅: {pct_str}\n'
                            f'时间: {now}'
                        )
                        show_popup_async(f'价格预警 - {name}', popup_msg)
                # 触发后继续监控，但降低频率到60秒；条件未解除不重复提醒
                time.sleep(60)
                continue
            else:
                alerted = False       # 条件解除，允许下次重新触发
                diff = price - target
                pct_str = f'{q["change_pct"]:+.2f}%' if q.get('change_pct') is not None else ''
                status = f'diff: {abs(diff):.2f}'
                if pct_str:
                    status += f' | {pct_str}'
                print(f'[{now}] #{check_count} {name} = {format_price(code, price)} '
                      f'({status}) [OK]')

        except Exception as e:
            now = datetime.datetime.now().strftime('%H:%M:%S')
            print(f'[{now}] Error: {e}')
            # tdx连接失败时尝试重连
            if source == 'tdx':
                close_tdx_client()
            time.sleep(interval)
            continue

        time.sleep(interval)


def _update_direction_latches(latches, key_prefix, below_hit, above_hit):
    """更新双向一次性预警锁存，返回本轮需要提醒的方向。

    每一侧独立锁存：命中且未提醒过 → 加入锁存并返回该方向；未命中 → 解除
    该方向锁存。这样价格从下方穿到上方再跌回下方时，下方仍会重新提醒，
    不会被上一轮的锁存永久压制。
    """
    fired = []
    for side, hit in (('below', below_hit), ('above', above_hit)):
        key = f'{key_prefix}_{side}'
        if hit:
            if key not in latches:
                latches.add(key)
                fired.append(side)
        else:
            latches.discard(key)
    return fired


def check_multi(config_file, source='tdx', host=None, notify='popup'):
    """从JSON配置文件监控多只股票"""
    # utf-8-sig：与 GUI 写出的带 BOM 配置保持一致，手改配置也不会因此启动失败
    with open(config_file, 'r', encoding='utf-8-sig') as f:
        config = json.load(f)

    interval = config.get('interval', 5)
    log_file = config.get('log') or get_log_path('multi')  # 空串也视为未配置，避免 open('') OSError
    items = config.get('alerts', [])

    source_label = f'TDX({host or DEFAULT_TDX_HOST})' if source == 'tdx' else 'Tencent'
    print(f'========================================')
    print(f'  MULTI-STOCK PRICE ALERT MONITOR v5.5.0')
    print(f'  Monitoring {len(items)} stock(s)')
    print(f'  Source: {source_label} | Interval: {interval}s')
    print(f'  Started: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print(f'========================================')
    print()

    # 一次性预警：条件持续为真只提醒一次；解除后再次满足才会重新提醒。
    alert_latches = set()

    while True:
        now_str = datetime.datetime.now().strftime('%H:%M:%S')

        for item in items:
            # 字段读取在 try 之外：配置项缺 code/target 或不是 dict 时 KeyError 会直接
            # 崩掉整个监控循环（后续股票再也不检查）。先校验、坏项跳过。
            if not isinstance(item, dict) or 'code' not in item or 'target' not in item:
                print(f'[{now_str}] 跳过无效配置项: {item!r}')
                continue
            code = item['code']
            target = item['target']
            direction = item.get('dir', 'below')
            name = item.get('name') or code
            alert_below = direction in ('below', 'both')
            alert_above = direction in ('above', 'both')

            try:
                q = get_quote(code, source, host)
                price = q.get('price')
                if price is None:
                    print(f'[{now_str}] {name}({code}) Price is None, skipping')
                    continue
                name = q.get('name', name)
                if not is_a_share_trading_time() or quote_is_stale(q):
                    _update_direction_latches(alert_latches, code, False, False)
                    print(f'[{now_str}] {name}({code}) 非交易时段或行情已过期，跳过预警')
                    continue

                below_hit = alert_below and price <= target
                above_hit = alert_above and price >= target

                if below_hit or above_hit:
                    # 按实际触发方向独立一次性提醒；未命中的方向解除锁存。
                    for side in _update_direction_latches(
                            alert_latches, code, below_hit, above_hit):
                        if side == 'below':
                            reason = (f'{name}({code}) 跌破 {format_price(code, target)} | '
                                      f'当前: {format_price(code, price)}')
                        else:
                            reason = (f'{name}({code}) 涨破 {format_price(code, target)} | '
                                      f'当前: {format_price(code, price)}')
                        alert_line = f'[{now_str}] *** ALERT *** {reason}'
                        print(alert_line)
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(alert_line + '\n')
                        if notify == 'popup':
                            show_popup_async('价格预警', reason)
                else:
                    _update_direction_latches(alert_latches, code, False, False)
                    diff = price - target
                    print(f'[{now_str}] {name} = {format_price(code, price)} '
                          f'(target: {format_price(code, target)}, '
                          f'diff: {abs(diff):.2f})')

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
        description='A股价格预警监控 v5.5.0（通达信协议/腾讯接口双源）',
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
