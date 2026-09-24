import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import price_alert  # noqa: E402


# 用假 opener 替换模块级 _HTTP_OPENER，避免真实网络请求
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


# ------------------------------------------------------------------
# P1：东财坏请求返回 {"data": null} —— 旧写法 data.get('data', {}).get(...) 会在
# try 之外抛 AttributeError，污染限流计数 / 刷错误日志
# ------------------------------------------------------------------
def test_eastmoney_batch_null_data_no_crash(monkeypatch):
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(b'{"data": null}'))
    assert price_alert.get_quotes_eastmoney_batch(['sh601398']) == {}


def test_eastmoney_batch_non_dict_body_no_crash(monkeypatch):
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(b'null'))
    assert price_alert.get_quotes_eastmoney_batch(['sh601398']) == {}


def test_kline_eastmoney_null_data_no_crash(monkeypatch):
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(b'{"data": null}'))
    assert price_alert._get_kline_eastmoney('sh601398', 5, 'daily') == []


def test_eastmoney_batch_normal_still_parses(monkeypatch):
    body = (b'{"data":{"diff":[{"f12":"601398","f13":1,"f2":7.5,"f3":1.2,'
            b'"f14":"ICBC","f15":7.6,"f16":7.4,"f17":7.45,"f18":7.4}]}}')
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(body))
    r = price_alert.get_quotes_eastmoney_batch(['sh601398'])
    assert 'sh601398' in r and r['sh601398']['price'] == 7.5


# ------------------------------------------------------------------
# P1：腾讯批量空 ret_code 时 endswith('') 恒真 → quote 误配给第一个未占用代码
# ------------------------------------------------------------------
def _tencent_line(inner):
    return ('v_x="' + inner + '";').encode('gbk', errors='replace')


def test_tencent_batch_empty_ret_code_not_misassigned(monkeypatch):
    # 段数够多、价格/涨跌幅齐全（不会被判残缺），但 ret_code 字段为空
    fields = [''] * 35
    fields[0], fields[1], fields[2], fields[3], fields[4] = '1', '垃圾名', '', '7.77', '7.00'
    fields[32] = '1.00'
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(_tencent_line('~'.join(fields))))
    r = price_alert.get_quotes_tencent_batch(['sh600000'])
    # 空 ret_code 不参与匹配 → 这条不该被错挂到 sh600000
    assert 'sh600000' not in r


def test_tencent_batch_normal_matches(monkeypatch):
    fields = [''] * 35
    fields[0], fields[1], fields[2], fields[3], fields[4] = '1', '工商银行', 'sh601398', '7.50', '7.40'
    fields[32], fields[33], fields[34] = '1.35', '7.60', '7.40'
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Opener(_tencent_line('~'.join(fields))))
    r = price_alert.get_quotes_tencent_batch(['sh601398'])
    assert 'sh601398' in r and r['sh601398']['price'] == 7.50


# ------------------------------------------------------------------
# P2：TDX 停牌/盘前 last_price=None —— 旧写法 None - pre_close 抛 TypeError
# ------------------------------------------------------------------
class _FakeQ:
    def __init__(self, last, pre):
        self.pre_close_price = pre
        self.last_price = last
        self.open_price = last
        self.high_price = last
        self.low_price = last
        self.total_hand = 0
        self.amount = 0
        self.buy_levels = []
        self.sell_levels = []


class _FakeClient:
    def __init__(self, q):
        self._q = q

    def get_quote(self, code):
        return [self._q]


def test_tdx_quote_none_last_price_no_typeerror(monkeypatch):
    monkeypatch.setattr(price_alert, 'get_tdx_client',
                        lambda host=None: _FakeClient(_FakeQ(None, 10.0)))
    out = price_alert.get_quote_tdx('sh600000')
    assert out['change_pct'] is None and out['price'] is None


def test_tdx_quote_normal_change_pct(monkeypatch):
    monkeypatch.setattr(price_alert, 'get_tdx_client',
                        lambda host=None: _FakeClient(_FakeQ(11.0, 10.0)))
    out = price_alert.get_quote_tdx('sh600000')
    assert out['change_pct'] == pytest.approx(10.0)
