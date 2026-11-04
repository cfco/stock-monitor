"""A 股数据层回归：代码归一、交易时段、源健康、坏行情过滤、K 线协议回退。"""
import datetime
import json
import math
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import price_alert  # noqa: E402


# ------------------------------------------------------------------
# 代码归一 / 北交所拒绝 / ETF 格式化
# ------------------------------------------------------------------
@pytest.mark.parametrize('raw, expected', [
    ('sh601398', 'sh601398'),
    ('SH601398', 'sh601398'),
    ('s_sh000001', 'sh000001'),
    (' S_SZ399001 ', 'sz399001'),
    ('bj430047', ''),
    ('hk00700', ''),
    ('600000', ''),
    ('sh60000', ''),
    ('', ''),
    (None, ''),
])
def test_normalize_a_share_code(raw, expected):
    assert price_alert.normalize_a_share_code(raw) == expected


def test_require_a_share_code_rejects_bj_with_clear_message():
    with pytest.raises(ValueError, match='北交所'):
        price_alert.require_a_share_code('bj430047')
    with pytest.raises(ValueError):
        price_alert.require_a_share_code('usAAPL')
    assert price_alert.require_a_share_code('SH601398') == 'sh601398'


def test_etf_price_format_uses_three_decimals():
    assert price_alert.is_etf_code('sh510300') is True
    assert price_alert.is_etf_code('sh601398') is False
    assert price_alert.price_decimals('sz159915') == 3
    assert price_alert.price_decimals('sh600000') == 2
    assert price_alert.format_price('sh510300', 1.23456) == '1.235'
    assert price_alert.format_price('sh600000', 1.23456) == '1.23'
    assert price_alert.format_price('sh600000', None) == '--'
    assert price_alert.format_price('sh600000', math.nan) == '--'
    assert price_alert.format_price('sh600000', 'bad') == '--'


def test_fund_code_families_cover_live_ranges():
    """按真实接口逐族核对过的 3 位小数号段，漏一族就会把 ETF/REIT 目标价显示错精度。

    这些代码不是编的：sh520550 / sh530000 / sh551520 / sz180101 都是在线行情，
    腾讯 f[3] 实测返回 3 位小数。此前只列了 sh50/51/56/58 + sz15/16，
    跨境 ETF、上证530000、科创债 ETF 和 REIT 会被四舍五入成 2 位。
    """
    three_decimal = ['sh501058', 'sh510050', 'sh520550', 'sh530000', 'sh551520',
                     'sh560010', 'sh588000', 'sz159915', 'sz161725', 'sz180101']
    for code in three_decimal:
        assert price_alert.price_decimals(code) == 3, code
        assert price_alert.format_price(code, 1.069) == '1.069', code

    # 个股仍是 2 位
    for code in ['sh600000', 'sz000001', 'sh601398']:
        assert price_alert.price_decimals(code) == 2, code


def test_detect_market_only_accepts_canonical_a_share():
    assert price_alert.detect_market('SH601398') == 'a_share'
    assert price_alert.detect_market('s_sh000001') == 'a_share'
    assert price_alert.detect_market('bj430047') == 'unknown'
    assert price_alert.detect_market('AAPL') == 'unknown'


# ------------------------------------------------------------------
# 交易时段 / 中国节假日
# ------------------------------------------------------------------
def test_trading_time_uses_shanghai_timezone_and_sessions():
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 11, 4, 9, 30)) is True
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 11, 4, 11, 30)) is True
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 11, 4, 12, 0)) is False
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 11, 4, 13, 0)) is True
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 11, 4, 15, 1)) is False

    # UTC 01:30 == 上海 09:30，不能被本机时区带偏
    utc = datetime.datetime(2026, 11, 4, 1, 30, tzinfo=datetime.timezone.utc)
    assert price_alert.is_a_share_trading_time(utc) is True


def test_trading_day_skips_weekends_and_cn_holidays():
    assert price_alert.is_a_share_trading_day(
        datetime.datetime(2026, 11, 4, 10, 0)) is True
    assert price_alert.is_a_share_trading_day(datetime.date(2026, 11, 4)) is True
    assert price_alert.is_a_share_trading_day(
        datetime.datetime(2026, 11, 7, 10, 0)) is False  # 周六
    # 2026 国庆休市
    assert price_alert.is_a_share_trading_day(
        datetime.datetime(2026, 10, 1, 10, 0)) is False
    assert price_alert.is_a_share_trading_time(
        datetime.datetime(2026, 10, 1, 10, 0)) is False
    # 2026 春节休市
    assert price_alert.is_a_share_trading_day(
        datetime.datetime(2026, 2, 17, 10, 0)) is False


# ------------------------------------------------------------------
# 数据源健康 & 坏行情过滤
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_source_state():
    def reset():
        for name in price_alert.A_SHARE_SOURCES:
            price_alert._source_recover(name)
        with price_alert._a_share_cursor_lock:
            price_alert._a_share_cursor = 0
    reset()
    yield
    reset()


def test_source_health_marks_disabled_then_recovers():
    for _ in range(price_alert.MAX_CONSECUTIVE_FAILS):
        price_alert._source_mark_fail('tencent', '(test)')
    health = price_alert.get_source_health()
    assert health['tencent']['available'] is False
    assert health['tencent']['disabled'] is True
    assert health['tencent']['retry_in'] > 0

    price_alert._source_mark_success('tencent')
    health = price_alert.get_source_health()
    assert health['tencent']['available'] is True
    assert health['tencent']['fail_count'] == 0


def test_source_disabled_ttl_auto_recovers():
    for _ in range(price_alert.MAX_CONSECUTIVE_FAILS):
        price_alert._source_mark_fail('sina', '(test)')
    # 把屏蔽时间拨到 TTL 之前，下一次查询应自动解封
    price_alert.SOURCE_DISABLED_TS['sina'] = (
        price_alert.time.time() - price_alert.SOURCE_DISABLE_DURATION - 1
    )
    assert price_alert._source_available('sina') is True
    assert 'sina' not in price_alert.SOURCE_DISABLED


def test_filter_valid_quotes_rejects_zero_negative_nan_and_nonfinite():
    quotes = {
        'sh600000': {'price': 0, 'change_pct': 1.0},
        'sh600001': {'price': -1, 'change_pct': 1.0},
        'sh600002': {'price': math.nan, 'change_pct': 1.0},
        'sh600003': {'price': math.inf, 'change_pct': 1.0},
        'sh600004': {'price': None, 'change_pct': 1.0},
        'sh600005': {'price': 10.0, 'change_pct': None},
        'sh600006': {'price': 10.0, 'change_pct': 1.0},
    }
    valid = price_alert._filter_valid_quotes(quotes)
    # 涨跌幅缺失的行情可以展示（显示 --），但源健康判定会另查 change_pct
    assert set(valid) == {'sh600005', 'sh600006'}
    assert price_alert._batch_has_valid_result(valid, ['sh600005']) is False
    assert price_alert._batch_has_valid_result(
        {'sh600006': valid['sh600006']}, ['sh600006']) is True


def test_invalid_primary_source_is_rejected_and_not_counted_as_success(monkeypatch):
    monkeypatch.setattr(
        price_alert, 'get_quotes_tencent_batch',
        lambda codes: {c: {'price': 0, 'change_pct': 1.0} for c in codes},
    )
    monkeypatch.setattr(price_alert, '_a_share_cursor', 0)

    result = price_alert.get_quotes_batch(['sh600000'])

    assert result == {}
    assert price_alert.SOURCE_FAIL_COUNTS['tencent'] == 1
    assert price_alert.get_source_health()['tencent']['available'] is True


def test_get_quotes_batch_maps_canonical_results_back_to_config_code(monkeypatch):
    quote = {'code': 'sh601398', 'name': '工商银行', 'price': 6.12,
             'yest_close': 6.0, 'change_pct': 2.0}
    monkeypatch.setattr(
        price_alert, 'get_quotes_tencent_batch',
        lambda codes: {c: dict(quote) for c in codes},
    )
    result = price_alert.get_quotes_batch(['SH601398', 's_sh601398'], source='tencent')
    assert set(result) == {'SH601398', 's_sh601398'}
    assert result['SH601398']['code'] == 'sh601398'


# ------------------------------------------------------------------
# 东方财富 K 线：HTTPS 被重置时回退 HTTP
# ------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StaticOpener:
    def __init__(self, payload):
        self._payload = payload

    def open(self, req, timeout=None):
        return _Resp(self._payload)


class _SchemeFallbackOpener:
    def __init__(self):
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if req.full_url.startswith('http://'):
            raise OSError('https handshake reset')
        return _Resp({'data': {'klines': [
            '2026-11-04 09:30,10.00,10.50,10.60,9.90,100',
        ]}})


def test_eastmoney_url_candidates_http_first_for_push2his():
    https_url = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?x=1'
    assert price_alert._eastmoney_url_candidates(https_url) == [
        'http://push2his.eastmoney.com/api/qt/stock/kline/get?x=1',
        https_url,
    ]
    assert price_alert._eastmoney_url_candidates('https://qt.gtimg.cn/q=x') == [
        'https://qt.gtimg.cn/q=x'
    ]


def test_kline_eastmoney_falls_back_from_http_reset_to_https(monkeypatch):
    opener = _SchemeFallbackOpener()
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', opener)

    rows = price_alert._get_kline_eastmoney('SH601398', count=1, period='1min')

    assert [u.split(':')[0] for u in opener.urls] == ['http', 'https']
    assert len(rows) == 1
    assert rows[0]['close'] == 10.5
    assert rows[0]['volume'] == 100.0


def test_tencent_kline_skips_bad_candle_without_shifting_dates(monkeypatch):
    """坏蜡烛跳过后，日期列表不能与蜡烛错位：否则 latest_day_only 会误删/错留。"""
    payload = {'data': {'sh600000': {'m1': [
        ['202611030930', '10', '10.1', '10.2', '9.9', '100'],
        ['202611040930', 'bad', '10', '10', '9', '100'],   # 日期字段有效，价格坏
    ]}}}
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _StaticOpener(payload))

    rows = price_alert._get_kline_tencent(
        'sh600000', count=2, period='1min', latest_day_only=True)

    assert len(rows) == 1
    assert rows[0]['time'] == '09:30'
    assert rows[0]['close'] == 10.1


def test_get_kline_falls_back_to_tencent_when_eastmoney_empty(monkeypatch):
    monkeypatch.setattr(price_alert, '_get_kline_eastmoney',
                        lambda *a, **k: [])
    monkeypatch.setattr(price_alert, '_get_kline_tencent',
                        lambda *a, **k: [{'time': '09:30', 'close': 1.0}])
    assert price_alert.get_kline('SH601398') == [{'time': '09:30', 'close': 1.0}]
