"""多源轮询扩展：华尔街见闻/同花顺/上交所官方解析 + 熔断与覆盖范围回归"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import price_alert as pa  # noqa: E402


class _Resp:
    def __init__(self, payload_bytes):
        self._d = payload_bytes

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, payload_bytes):
        self._d = payload_bytes

    def open(self, req, timeout=None, data=None):
        return _Resp(self._d)


@pytest.fixture
def patch_http(monkeypatch):
    def _set(payload):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        monkeypatch.setattr(pa, '_HTTP_OPENER', _Opener(payload))
    return _set


@pytest.fixture(autouse=True)
def _clean_source_state():
    for s in pa.A_SHARE_SOURCES:
        pa._source_recover(s)
    pa._a_share_cursor = 0
    yield
    for s in pa.A_SHARE_SOURCES:
        pa._source_recover(s)
    pa._a_share_cursor = 0


# ============================================================
# 华尔街见闻（样本取自 2026-11-19 盘中真实返回）
# ============================================================
WSCN_PAYLOAD = json.dumps({
    'code': 20000, 'message': 'OK',
    'data': {
        'fields': ['prod_name', 'last_px', 'open_px', 'high_px', 'low_px',
                   'close_px', 'px_change_rate', 'update_time', 'delisting_date'],
        'snapshot': {
            '601398.SS': ['工商银行', 9.05, 9.07, 9.09, 9.03, 0,
                          -0.11037527593818748, 1795052004, 0],
            '000001.SZ': ['平安银行', 12.22, 12.2, 12.26, 12.17, 0,
                          0.1639344262295193, 1795052007, 0],
        },
    },
}, ensure_ascii=False)


def test_wscn_batch_parses_and_derives_yest(patch_http):
    patch_http(WSCN_PAYLOAD)
    r = pa.get_quotes_wscn_batch(['sh601398', 'sz000001'])
    assert set(r) == {'sh601398', 'sz000001'}
    icbc = r['sh601398']
    assert icbc['name'] == '工商银行'
    assert icbc['price'] == 9.05
    assert icbc['change_pct'] == pytest.approx(-0.110375, rel=1e-4)
    assert icbc['quote_ts'] == 1795052004
    assert icbc['yest_close'] == pytest.approx(9.06, abs=1e-6)
    assert icbc['open'] == 9.07 and icbc['high'] == 9.09 and icbc['low'] == 9.03


def test_wscn_maps_by_field_name_not_position(patch_http):
    obj = json.loads(WSCN_PAYLOAD)
    pairs = list(zip(obj['data']['fields'], obj['data']['snapshot']['601398.SS']))
    pairs.reverse()
    obj['data']['fields'] = [p[0] for p in pairs]
    obj['data']['snapshot']['601398.SS'] = [p[1] for p in pairs]
    patch_http(json.dumps(obj, ensure_ascii=False))
    r = pa.get_quotes_wscn_batch(['sh601398'])
    assert r['sh601398']['price'] == 9.05
    assert r['sh601398']['name'] == '工商银行'


def test_wscn_null_data_no_crash(patch_http):
    patch_http('{"data": null}')
    assert pa.get_quotes_wscn_batch(['sh601398']) == {}


def test_wscn_zero_price_skipped(patch_http):
    obj = json.loads(WSCN_PAYLOAD)
    obj['data']['snapshot']['601398.SS'][1] = 0
    patch_http(json.dumps(obj, ensure_ascii=False))
    r = pa.get_quotes_wscn_batch(['sh601398', 'sz000001'])
    assert 'sh601398' not in r and 'sz000001' in r


def test_wscn_missing_last_px_field_returns_empty(patch_http):
    patch_http('{"data": {"fields": ["prod_name"], "snapshot": {"601398.SS": ["工商银行"]}}}')
    assert pa.get_quotes_wscn_batch(['sh601398']) == {}


# ============================================================
# 同花顺 realhead（键映射盘中对腾讯基准核验）
# ============================================================
THS_PAYLOAD = ('quotebridge_v2_realhead_hs_601398_last(' + json.dumps({
    'items': {'6': '9.06', '7': '9.07', '8': '9.09', '9': '9.03',
              '10': '9.05', '24': '9.05', '25': '142600.00',
              '30': '9.06', '31': '207954.00', '13': '16980545.00',
              '19': '153937020.00', '199112': '-0.11'},
    'name': '工商银行', 'time': '2026-11-19 09:33:13 北京时间',
}, ensure_ascii=False) + ')')


def test_10jqka_single_mapping(patch_http):
    patch_http(THS_PAYLOAD)
    q = pa.get_quote_10jqka('sh601398')
    assert q['name'] == '工商银行'
    assert q['price'] == 9.05
    assert q['yest_close'] == 9.06
    assert q['open'] == 9.07 and q['high'] == 9.09 and q['low'] == 9.03
    assert q['change_pct'] == pytest.approx(-0.110375, rel=1e-3)
    assert q['quote_ts'] == pa._parse_cn_quote_time('2026-11-19 09:33:13 北京时间')


def test_10jqka_name_inside_items(patch_http):
    # 实测（2026-11-19 盘中）：响应顶层只有 items，name 在 items 内
    payload = ('quotebridge_v2_realhead_hs_601398_last(' + json.dumps({
        'items': {'6': '9.06', '10': '9.05', 'name': '工商银行'},
    }, ensure_ascii=False) + ')')
    patch_http(payload)
    q = pa.get_quote_10jqka('sh601398')
    assert q['name'] == '工商银行'
    assert q['price'] == 9.05


def test_10jqka_preopen_falls_back_to_bid1(patch_http):
    obj = json.loads(THS_PAYLOAD[THS_PAYLOAD.index('(') + 1:THS_PAYLOAD.rindex(')')])
    obj['items']['10'] = '0'
    patch_http('x(' + json.dumps(obj, ensure_ascii=False) + ')')
    q = pa.get_quote_10jqka('sh601398')
    assert q['price'] == 9.05  # 买1价 24 兜底


def test_10jqka_malformed_jsonp_raises(patch_http):
    patch_http('not a jsonp response')
    with pytest.raises(ValueError):
        pa.get_quote_10jqka('sh601398')


def test_10jqka_batch_covers_both_markets(patch_http):
    patch_http(THS_PAYLOAD)
    r = pa.get_quotes_10jqka_batch(['sh601398', 'SZ000001'])
    assert set(r) == {'sh601398', 'sz000001'}


def test_10jqka_batch_stops_after_two_consecutive_errors(monkeypatch):
    calls = []

    def _boom(code):
        calls.append(code)
        raise OSError('节点挂了')
    monkeypatch.setattr(pa, 'get_quote_10jqka', _boom)
    r = pa.get_quotes_10jqka_batch(['sh601398', 'sh600519', 'sz000001'])
    assert r == {}
    assert len(calls) == 2  # 第三只不再请求，交给腾讯补齐


@pytest.mark.parametrize(('code', 'symbol'), [
    ('sh000001', '1A0001'),   # 上证指数：按 000001 请求会命中平安银行
    ('sh000300', '1B0300'),   # 沪深300
    ('sh000016', '1B0016'),   # 上证50
    ('sh000688', '1B0688'),   # 科创50
    ('sz399001', '399001'),   # 深证成指
    ('sz399006', '399006'),   # 创业板指
    ('sh601398', '601398'),   # 个股保持原数字
    ('sz000001', '000001'),   # 深市个股与沪市指数同号，不能加前缀
])
def test_10jqka_symbol_mapping(code, symbol):
    """回归：指数必须用同花顺内部编码，避免与同号个股混淆。"""
    assert pa._ths_realhead_symbol(code) == symbol


def test_10jqka_index_request_uses_1A_symbol(monkeypatch):
    """sh000001 实际请求 1A0001，且解析出的点位是 3500 级别而非 11 元。"""
    seen = []

    class _CaptureOpener:
        def open(self, req, timeout=None, data=None):
            seen.append(req.full_url)
            payload = ('quotebridge_v2_realhead_hs_1A0001_last(' + json.dumps({
                'items': {'6': '3577.62', '7': '3527.30', '8': '3534.15',
                          '9': '3526.84', '10': '3527.78'},
                'name': '上证指数',
            }, ensure_ascii=False) + ')').encode('utf-8')
            return _Resp(payload)

    monkeypatch.setattr(pa, '_HTTP_OPENER', _CaptureOpener())
    q = pa.get_quote_10jqka('sh000001')
    assert 'hs_1A0001' in seen[0]
    assert q['name'] == '上证指数'
    assert q['price'] == 3527.78


# ============================================================
# 上交所官方 yunhq（仅沪市）
# ============================================================
SSE_PAYLOAD = json.dumps({
    'code': '601398', 'date': 20261119, 'time': 93251,
    'snap': ['工商银行', 9.06, 9.07, 9.09, 9.03, 9.05, -0.01, -0.11,
             26154999, 237031314,
             [9.05, 24500, 9.04, 65300, 9.03, 402555, 9.02, 419500, 9.01, 667100],
             [9.06, 192854, 9.07, 609400, 9.08, 714900, 9.09, 985200, 9.10, 2032200]],
}, ensure_ascii=False)


def test_sse_single_mapping(patch_http):
    patch_http(SSE_PAYLOAD)
    q = pa.get_quote_sse('sh601398')
    assert q['name'] == '工商银行'
    assert q['price'] == 9.05
    assert q['yest_close'] == 9.06
    assert q['open'] == 9.07 and q['high'] == 9.09 and q['low'] == 9.03
    assert q['change_pct'] == pytest.approx(-0.110375, rel=1e-3)
    assert q['quote_ts'] == pa._cn_timestamp(20261119, 93251)


def test_sse_rejects_sz(patch_http):
    patch_http(SSE_PAYLOAD)
    with pytest.raises(ValueError):
        pa.get_quote_sse('sz000001')


def test_sse_short_snap_raises(patch_http):
    patch_http('{"snap": ["工商银行", 9.06]}')
    with pytest.raises(ValueError):
        pa.get_quote_sse('sh601398')


def test_sse_batch_skips_sz(patch_http):
    patch_http(SSE_PAYLOAD)
    r = pa.get_quotes_sse_batch(['sh601398', 'sz000001'])
    assert set(r) == {'sh601398'}


# ============================================================
# 熔断与轮询接线
# ============================================================
def _valid_rec(code, price=9.05):
    return {'name': '测试', 'code': code, 'price': price, 'yest_close': 9.06,
            'open': 9.07, 'high': 9.09, 'low': 9.03, 'change_pct': -0.11}


def test_new_sources_registered_with_labels():
    for s in ('wscn', '10jqka', 'sse'):
        assert s in pa.A_SHARE_SOURCES
    health = pa.get_source_health()
    assert health['wscn']['label'] == '华尔街见闻'
    assert health['10jqka']['label'] == '同花顺'
    assert health['sse']['label'] == '上交所官方'


def test_new_source_banned_after_5_fails():
    for _ in range(4):
        pa._source_mark_fail('wscn')
    assert pa._source_available('wscn') is True
    pa._source_mark_fail('wscn')
    assert pa._source_available('wscn') is False
    assert pa.get_source_health()['wscn']['retry_in'] > 0


def test_ban_expires_after_300s(monkeypatch):
    for _ in range(5):
        pa._source_mark_fail('10jqka')
    assert pa._source_available('10jqka') is False
    real_time = pa.time.time()
    monkeypatch.setattr(pa.time, 'time', lambda: real_time + 301)
    assert pa._source_available('10jqka') is True
    assert pa.SOURCE_FAIL_COUNTS['10jqka'] == 0


def test_auto_skips_banned_sources(monkeypatch):
    for s in pa.A_SHARE_SOURCES:
        if s != 'wscn':
            for _ in range(5):
                pa._source_mark_fail(s)
    for s in pa.A_SHARE_SOURCES:
        if s != 'wscn':
            monkeypatch.setattr(pa, f'get_quotes_{s}_batch',
                                lambda *a, **k: (_ for _ in ()).throw(
                                    AssertionError('被屏蔽源不应被调用')))
    monkeypatch.setattr(pa, 'get_quotes_wscn_batch',
                        lambda codes: {c: _valid_rec(c) for c in codes})
    r = pa.get_quotes_batch(['sh601398', 'sz000001'], source='auto')
    assert set(r) == {'sh601398', 'sz000001'}
    assert r['sh601398']['price'] == 9.05


def test_sse_round_covers_sh_and_tencent_fills_sz(monkeypatch):
    # 只留 sse 与 tencent 可用，游标拨到 sse
    for s in pa.A_SHARE_SOURCES:
        if s not in ('sse', 'tencent'):
            for _ in range(5):
                pa._source_mark_fail(s)
    pa._a_share_cursor = 1  # available=['tencent','sse'] → idx 1 命中 sse
    monkeypatch.setattr(pa, 'get_quotes_sse_batch',
                        lambda codes: {c: _valid_rec(c) for c in codes
                                       if c.startswith('sh')})
    monkeypatch.setattr(pa, 'get_quotes_tencent_batch',
                        lambda codes: {c: _valid_rec(c, 12.22) for c in codes})
    r = pa.get_quotes_batch(['sh601398', 'sz000001'], source='auto')
    assert r['sh601398']['price'] == 9.05
    assert r['sz000001']['price'] == 12.22  # 深市由腾讯补齐
    assert pa.SOURCE_FAIL_COUNTS.get('sse', 0) == 0


def test_pure_sz_list_never_charges_sse(monkeypatch):
    for s in pa.A_SHARE_SOURCES:
        if s != 'sse':
            for _ in range(5):
                pa._source_mark_fail(s)
    # 纯深市：sse 被覆盖过滤排除（含退化列表），其余源虽被屏蔽仍按既有
    # 退化逻辑轮询；这里全部 mock 掉避免真实网络，只验证 sse 不被请求也不计失败
    for s in pa.A_SHARE_SOURCES:
        monkeypatch.setattr(pa, f'get_quotes_{s}_batch', lambda *a, **k: {})
    r = pa.get_quotes_batch(['sz000001'], source='auto')
    assert r == {}
    assert pa.SOURCE_FAIL_COUNTS.get('sse', 0) == 0


def test_odd_batch_requires_ceiling_half():
    good = {'price': 10.0, 'change_pct': 1.0}
    assert pa._batch_has_valid_result({'sh600000': good}, ['sh600000']) is True
    assert pa._batch_has_valid_result(
        {'sh600000': good}, ['sh600000', 'sh600001', 'sh600002']) is False
    assert pa._batch_has_valid_result(
        {'sh600000': good, 'sh600001': good},
        ['sh600000', 'sh600001', 'sh600002']) is True


def test_missing_change_pct_is_filled_from_backup_tencent(monkeypatch):
    pa._a_share_cursor = 0  # 主源腾讯
    monkeypatch.setattr(pa, 'get_quotes_tencent_batch', lambda codes: {
        c: {'code': c, 'price': 10.0, 'change_pct': None} for c in codes})
    monkeypatch.setattr(pa, 'get_quotes_tencent_sqt_batch', lambda codes: {
        c: {'code': c, 'price': 10.0, 'change_pct': 2.0} for c in codes})

    result = pa.get_quotes_batch(['sh600000'])

    assert result['sh600000']['change_pct'] == 2.0
    assert pa.SOURCE_FAIL_COUNTS['tencent'] == 1
    assert pa.SOURCE_FAIL_COUNTS['tencent_sqt'] == 0


def test_fallback_failure_is_counted(monkeypatch):
    pa._a_share_cursor = pa.A_SHARE_SOURCES.index('wscn')
    monkeypatch.setattr(pa, 'get_quotes_wscn_batch', lambda codes: {
        'sh601398': _valid_rec('sh601398')})
    monkeypatch.setattr(pa, 'get_quotes_tencent_batch', lambda codes: {})

    pa.get_quotes_batch(['sh601398', 'sz000001'])

    assert pa.SOURCE_FAIL_COUNTS['tencent'] == 1


def test_all_sources_disabled_keeps_cooldown_without_requests(monkeypatch):
    for source in pa.A_SHARE_SOURCES:
        for _ in range(pa.MAX_CONSECUTIVE_FAILS):
            pa._source_mark_fail(source)
    for source in pa.A_SHARE_SOURCES:
        monkeypatch.setattr(
            pa, f'get_quotes_{source}_batch',
            lambda *a, **k: (_ for _ in ()).throw(AssertionError('冷却源不应请求')))

    assert pa.get_quotes_batch(['sh601398']) == {}


def test_forced_source_bypasses_ban(monkeypatch):
    for _ in range(5):
        pa._source_mark_fail('tencent')
    assert pa._source_available('tencent') is False
    monkeypatch.setattr(pa, 'get_quotes_tencent_batch',
                        lambda codes: {c: _valid_rec(c) for c in codes})
    r = pa.get_quotes_batch(['sh601398'], source='tencent')
    assert r['sh601398']['price'] == 9.05