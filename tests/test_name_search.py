"""名称反查代码：腾讯 smartbox 解析 / Alpaca assets 反查 / 设置页接线回归"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, 'scripts')
for _p in (_ROOT, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

import price_alert
import alpaca_history as al


# ============================================================
# 腾讯 smartbox 解析（mock _HTTP_OPENER，不走真实网络）
# ============================================================
class _FakeOpener:
    def __init__(self, payload):
        self._payload = payload

    def open(self, req, timeout=None, data=None):
        return _FakeResponse(self._payload)


class _FakeResponse:
    def __init__(self, payload):
        self._data = payload.encode('gbk', errors='replace')

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _u(text):
    """smartbox v=2 把中文写成 \\uXXXX 字面量"""
    return ''.join('\\u%04x' % ord(c) for c in text)


@pytest.fixture
def patch_http(monkeypatch):
    def _set(payload):
        monkeypatch.setattr(price_alert, '_HTTP_OPENER', _FakeOpener(payload))
    return _set


def test_smartbox_multi_match_with_unicode_names(patch_http):
    payload = ('v_hint="sh~600519~' + _u('贵州茅台') + '~gzmt~GP-A^'
               'sz~000001~' + _u('平安银行') + '~payh~GP-A";')
    patch_http(payload)
    assert price_alert.search_stocks_by_name('茅台') == [
        ('sh600519', '贵州茅台'), ('sz000001', '平安银行')]


def test_smartbox_plain_gbk_name_also_parsed(patch_http):
    patch_http('v_hint="sz~000001~平安银行~payh~GP-A";')
    assert price_alert.search_stocks_by_name('平安') == [('sz000001', '平安银行')]


def test_smartbox_filters_unsupported_markets(patch_http):
    payload = ('v_hint="hk~00700~' + _u('腾讯控股') + '~tx~GP^'
               'bj~920001~' + _u('北交样例') + '~bj~GP-A^'
               'us~AAPL~' + _u('苹果') + '~pg~GP^'
               'sh~600519~' + _u('贵州茅台') + '~gzmt~GP-A";')
    patch_http(payload)
    assert price_alert.search_stocks_by_name('样例') == [('sh600519', '贵州茅台')]


def test_smartbox_skips_malformed_entries(patch_http):
    payload = ('v_hint="sh~600519^'                      # 只有 2 段
               'sh~abc123~' + _u('坏代码') + '~h~GP-A^'   # 代码非 6 位数字
               'sz~000001~' + _u('平安银行') + '~payh~GP-A";')
    patch_http(payload)
    assert price_alert.search_stocks_by_name('平安') == [('sz000001', '平安银行')]


def test_smartbox_empty_or_missing_hint(patch_http):
    patch_http('v_hint="";')
    assert price_alert.search_stocks_by_name('不存在') == []
    patch_http('v_hint=;')
    assert price_alert.search_stocks_by_name('不存在') == []


def test_smartbox_empty_query_skips_network(monkeypatch):
    class _Boom:
        def open(self, *a, **k):
            raise AssertionError('空查询不应发起网络请求')
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Boom())
    assert price_alert.search_stocks_by_name('  ') == []


def test_smartbox_network_error_propagates(monkeypatch):
    class _Boom:
        def open(self, *a, **k):
            raise OSError('网络不可达')
    monkeypatch.setattr(price_alert, '_HTTP_OPENER', _Boom())
    with pytest.raises(OSError):
        price_alert.search_stocks_by_name('茅台')


# ============================================================
# Alpaca assets 名称反查（mock 缓存与凭证，不走真实网络）
# ============================================================
@pytest.fixture
def patch_assets(monkeypatch):
    def _set(cache, creds=True):
        monkeypatch.setattr(al, 'has_credentials', lambda: creds)
        monkeypatch.setattr(al, '_assets_cache', dict(cache))
        monkeypatch.setattr(al, '_assets_loaded', True)
        monkeypatch.setattr(al, '_assets_ts', time.time())
    return _set


def test_alpaca_no_credentials_returns_empty(patch_assets):
    patch_assets({'AAPL': 'Apple Inc. Common Stock'}, creds=False)
    assert al.search_symbols_by_name('apple') == []


def test_alpaca_case_insensitive_substring(patch_assets):
    patch_assets({
        'AAPL': 'Apple Inc. Common Stock',
        'MSFT': 'Microsoft Corporation',
    })
    assert al.search_symbols_by_name('APPLE') == [('AAPL', 'Apple Inc. Common Stock')]
    assert al.search_symbols_by_name('soft') == [('MSFT', 'Microsoft Corporation')]


def test_alpaca_prefix_matches_first(patch_assets):
    patch_assets({
        'ZZZ': 'The Apple Pie Company',
        'AAPL': 'Apple Inc. Common Stock',
    })
    res = al.search_symbols_by_name('apple')
    assert [sym for sym, _ in res] == ['AAPL', 'ZZZ']


def test_alpaca_limit(patch_assets):
    patch_assets({f'S{i:03d}': f'Apple Number {i}' for i in range(30)})
    assert len(al.search_symbols_by_name('apple')) == 20
    assert len(al.search_symbols_by_name('apple', limit=5)) == 5


def test_alpaca_lazy_load_when_cache_empty(monkeypatch):
    monkeypatch.setattr(al, 'has_credentials', lambda: True)
    monkeypatch.setattr(al, '_assets_cache', {})
    monkeypatch.setattr(al, '_assets_loaded', False)
    monkeypatch.setattr(al, '_assets_ts', 0.0)

    def _fake_load():
        with al._assets_lock:
            al._assets_cache['AAPL'] = 'Apple Inc. Common Stock'
            al._assets_loaded = True
            al._assets_ts = time.time()
    monkeypatch.setattr(al, '_load_assets', _fake_load)
    assert al.search_symbols_by_name('apple') == [('AAPL', 'Apple Inc. Common Stock')]


# ============================================================
# 解析线程与设置页接线（Qt offscreen）
# ============================================================
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

import floating_window as fw  # noqa: E402


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(fw, 'search_stocks_by_name', lambda q: [])
    monkeypatch.setattr(fw.alpaca_history, 'search_symbols_by_name', lambda q, limit=20: [])


def _run_thread(text):
    th = fw._StockResolveThread(text)
    got = []
    th.finished_resolve.connect(got.extend)
    th.run()
    return got


def test_thread_chinese_name_falls_back_to_name_search(qapp, monkeypatch, no_network):
    monkeypatch.setattr(fw, 'search_stocks_by_name', lambda q: [('sh600519', '贵州茅台')])
    assert _run_thread('贵州茅台') == [('sh600519', '贵州茅台')]


def test_thread_name_search_survives_smartbox_failure(qapp, monkeypatch, no_network):
    def _boom(q):
        raise OSError('smartbox 挂了')
    monkeypatch.setattr(fw, 'search_stocks_by_name', _boom)
    monkeypatch.setattr(fw.alpaca_history, 'search_symbols_by_name',
                        lambda q, limit=20: [('MSFT', 'Microsoft Corporation')])
    # 用超过 5 位的英文名，避免被当成美股代码（1-5 位字母）走代码分支
    assert _run_thread('microsoft') == [('MSFT', 'Microsoft Corporation')]


def test_thread_code_path_unchanged(qapp, monkeypatch, no_network):
    monkeypatch.setattr(price_alert, 'get_quote_tencent',
                        lambda code: {'name': '贵州茅台', 'price': 100.0})
    monkeypatch.setattr(fw, 'search_stocks_by_name',
                        lambda q: (_ for _ in ()).throw(AssertionError('代码输入不应走名称搜索')))
    assert _run_thread('sh600519') == [('sh600519', '贵州茅台')]


def _wait_resolve(dlg, qapp):
    th = dlg._resolve_thread
    assert th is not None, '应已启动解析线程'
    assert th.wait(3000), '解析线程超时'
    qapp.processEvents()


def test_settings_name_column_fills_code(qapp, monkeypatch, no_network):
    monkeypatch.setattr(fw, 'search_stocks_by_name', lambda q: [('sh600519', '贵州茅台')])
    dlg = fw.SettingsDialog({'alerts': []})
    dlg._add_row()
    row = dlg.table.rowCount() - 1
    dlg.table.item(row, 1).setText('茅台')
    _wait_resolve(dlg, qapp)
    assert dlg.table.item(row, 0).text() == 'sh600519'
    assert dlg.table.item(row, 1).text() == '贵州茅台'
    dlg.deleteLater()


def test_settings_name_column_multi_candidates_dialog(qapp, monkeypatch, no_network):
    monkeypatch.setattr(fw, 'search_stocks_by_name',
                        lambda q: [('sh600519', '贵州茅台'), ('sz000001', '平安银行')])
    monkeypatch.setattr(QInputDialog, 'getItem',
                        lambda *a, **k: ('sz000001  平安银行', True))
    dlg = fw.SettingsDialog({'alerts': []})
    dlg._add_row()
    row = dlg.table.rowCount() - 1
    dlg.table.item(row, 1).setText('银行')
    _wait_resolve(dlg, qapp)
    assert dlg.table.item(row, 0).text() == 'sz000001'
    assert dlg.table.item(row, 1).text() == '平安银行'
    dlg.deleteLater()


def test_settings_name_edit_with_existing_code_does_not_resolve(qapp, monkeypatch, no_network):
    dlg = fw.SettingsDialog({'alerts': [{'code': 'sh601398', 'name': '工商银行'}]})
    dlg.table.item(0, 1).setText('我的备注名')
    assert dlg._resolve_thread is None
    assert dlg.table.item(0, 0).text() == 'sh601398'
    dlg.deleteLater()


def test_settings_code_column_chinese_name_fills_both(qapp, monkeypatch, no_network):
    monkeypatch.setattr(fw, 'search_stocks_by_name', lambda q: [('sh600519', '贵州茅台')])
    dlg = fw.SettingsDialog({'alerts': []})
    dlg._add_row()
    row = dlg.table.rowCount() - 1
    dlg.table.item(row, 0).setText('茅台')
    _wait_resolve(dlg, qapp)
    assert dlg.table.item(row, 0).text() == 'sh600519'
    assert dlg.table.item(row, 1).text() == '贵州茅台'
    dlg.deleteLater()


def test_settings_name_column_accepts_code_too(qapp, monkeypatch, no_network):
    def _quote(code):
        if code == 'sh600519':
            return {'name': '贵州茅台', 'price': 100.0}
        raise ValueError('无行情')
    monkeypatch.setattr(price_alert, 'get_quote_tencent', _quote)
    dlg = fw.SettingsDialog({'alerts': []})
    dlg._add_row()
    row = dlg.table.rowCount() - 1
    dlg.table.item(row, 1).setText('600519')
    _wait_resolve(dlg, qapp)
    assert dlg.table.item(row, 0).text() == 'sh600519'
    assert dlg.table.item(row, 1).text() == '贵州茅台'
    dlg.deleteLater()