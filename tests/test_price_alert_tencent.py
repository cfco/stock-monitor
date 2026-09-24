import os
import sys

import pytest

# 注入 scripts/ 到 sys.path，使 import price_alert 可用（不依赖 Qt / eltdx）
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import price_alert  # noqa: E402


class _FakeOpener:
    """替代模块级 _HTTP_OPENER，返回预设行情文本，避免真实网络请求。"""

    def __init__(self, payload):
        self._payload = payload

    def open(self, req, timeout=None, data=None):
        return _FakeResponse(self._payload)


class _FakeResponse:
    def __init__(self, payload):
        # 腾讯接口以 gbk 编码返回；errors='replace' 兜底异常字符
        self._data = payload.encode('gbk', errors='replace')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _build_inner(name, code, price, yest, opn, chg, high, low):
    """构造腾讯内部 ~ 分隔串（索引0为前缀'1'，与真实接口一致）。"""
    fields = [''] * 35
    fields[0] = '1'
    fields[1] = name
    fields[2] = code
    fields[3] = str(price)
    fields[4] = str(yest)
    fields[5] = str(opn)
    fields[32] = str(chg)
    fields[33] = str(high)
    fields[34] = str(low)
    return '~'.join(fields)


def _single_payload(code, **kw):
    inner = _build_inner(
        kw.get('name', '测试'),
        code,
        kw.get('price', 10.5),
        kw.get('yest', 10.0),
        kw.get('opn', 10.1),
        kw.get('chg', 5.0),
        kw.get('high', 11.0),
        kw.get('low', 9.8),
    )
    return f'v_{code}="{inner}";'


def _multi_payload(records):
    return ''.join(
        f'v_{code}="{_build_inner(name, code, price, yest, opn, chg, high, low)}";'
        for (code, name, price, yest, opn, chg, high, low) in records
    )


@pytest.fixture
def patch_http(monkeypatch):
    def _set(payload):
        monkeypatch.setattr(price_alert, '_HTTP_OPENER', _FakeOpener(payload))

    return _set


def test_single_quote(patch_http):
    payload = _single_payload('sh601398', name='工商银行', price=6.12, yest=6.00,
                              opn=6.01, chg=2.0, high=6.20, low=5.95)
    patch_http(payload)
    q = price_alert.get_quote_tencent('sh601398')
    assert q['code'] == 'sh601398'
    assert q['name'] == '工商银行'
    assert q['price'] == 6.12
    assert q['yest_close'] == 6.00
    assert q['open'] == 6.01
    assert q['change_pct'] == 2.0
    assert q['high'] == 6.20
    assert q['low'] == 5.95


def test_batch_quote(patch_http):
    records = [
        ('sh601398', '工商银行', 6.12, 6.00, 6.01, 2.0, 6.20, 5.95),
        ('sz000001', '平安银行', 12.34, 12.00, 12.10, 2.83, 12.5, 11.9),
    ]
    patch_http(_multi_payload(records))
    res = price_alert.get_quotes_tencent_batch(['sh601398', 'sz000001'])
    assert set(res.keys()) == {'sh601398', 'sz000001'}
    assert res['sh601398']['price'] == 6.12
    assert res['sz000001']['name'] == '平安银行'


def test_sqt_batch_quote(patch_http):
    records = [
        ('sh601398', '工商银行', 6.12, 6.00, 6.01, 2.0, 6.20, 5.95),
        ('sz000001', '平安银行', 12.34, 12.00, 12.10, 2.83, 12.5, 11.9),
    ]
    patch_http(_multi_payload(records))
    res = price_alert.get_quotes_tencent_sqt_batch(['sh601398', 'sz000001'])
    assert set(res.keys()) == {'sh601398', 'sz000001'}
    assert res['sh601398']['price'] == 6.12
    assert res['sz000001']['change_pct'] == 2.83


def test_single_malformed_raises(patch_http):
    # 畸形响应：内部串仅 3 段，不够解析 → 不应抛 IndexError，应抛 ValueError（L9 防御）
    payload = 'v_sh601398="1~工商银行~sh601398";'
    patch_http(payload)
    with pytest.raises(ValueError):
        price_alert.get_quote_tencent('sh601398')


def test_batch_malformed_skipped(patch_http):
    # 一条正常 + 一条畸形，畸形应被跳过而非崩溃
    good = 'v_sh601398="' + _build_inner('工商银行', 'sh601398',
                                         6.12, 6.00, 6.01, 2.0, 6.20, 5.95) + '";'
    bad = 'v_sz000001="1~平安银行~sz000001";'
    patch_http(good + bad)
    res = price_alert.get_quotes_tencent_batch(['sh601398', 'sz000001'])
    assert 'sh601398' in res
    assert 'sz000001' not in res


def test_non_a_share_rejected(patch_http):
    # 非 A 股代码应直接被校验拦截（不依赖网络）
    with pytest.raises(ValueError):
        price_alert.get_quote_tencent('usAAPL')
