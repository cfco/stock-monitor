"""现价口径回归：盘前/盘后的宽价差中值不能当现价（会显示假涨跌、误触目标价预警）

数值取自 2026-10-29 实测：ET 05:28（盘前）AAPL bp=325.91 / ap=360.46 / p=None，
昨收 347.10 —— 旧口径取中值 343.19，界面会显示 -1.13% 的假跌幅。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import alpaca_history as H
import alpaca_stream as S


def test_last_trade_beats_mid():
    assert H.pick_quote_price(325.91, 360.46, 344.00, 347.10) == 344.0


def test_wide_spread_mid_is_rejected():
    assert H.pick_quote_price(325.91, 360.46, None, 347.10) is None


def test_narrow_spread_mid_is_accepted():
    assert H.pick_quote_price(347.00, 347.10, None, 347.10) == round((347.00 + 347.10) / 2, 4)


def test_single_sided_quote_still_accepted():
    """只有一条边时无法判价差宽窄，维持原行为（有真实挂单，好过没有）"""
    assert H.pick_quote_price(347.00, 0, None, 347.10) == 347.0


def test_without_reference_price_keeps_old_behaviour():
    assert H.pick_quote_price(10.0, 10.2, None, None) == 10.1


def test_bad_payloads_do_not_raise():
    assert H._quote_price(None) is None
    assert H._quote_price({'bp': 'x', 'ap': 'y'}) is None
    assert H.pick_quote_price(None, None, 'bad', 0) is None


def test_ws_wide_spread_quote_does_not_touch_cache():
    th = S._AlpacaStreamThread()
    H._set_prev_close('ZTEST', 347.10)
    th._handle_item({'T': 'q', 'sym': 'ZTEST', 'bp': 325.91, 'ap': 360.46, 't': 'x'})
    assert S._read_quote('ZTEST') is None            # 宽价差报价被丢弃

    th._handle_item({'T': 't', 'sym': 'ZTEST', 'p': 346.50, 't': 'y'})
    assert S._read_quote('ZTEST')['price'] == 346.5  # 成交价照常写入


def test_same_day_filter_drops_previous_session_bars():
    """盘前今日不足 count 根时，desc 取数会补进上一交易日的 bar，
    而分时标签只有 HH:MM → 图上出现两根同一时刻分属两天"""
    rows = [
        {'t': '2026-10-27T20:59:00Z', 'c': 1},    # 10-27 收盘前
        {'t': '2026-10-27T14:30:00Z', 'c': 1},    # 10-27 盘中
        {'t': '2026-10-26T19:00:00Z', 'c': 1},    # 上一交易日，必须被滤掉
    ]
    kept = H._same_day_bars(rows)
    assert len(kept) == 2
    assert all(r['t'].startswith('2026-10-27') for r in kept)


def test_bad_bar_does_not_take_down_the_whole_batch():
    """一只票的坏 bar 不能让整批美股当轮没行情（原先是 bars[1]['c'] 裸下标）"""
    prev, latest = H._resolve_prev_close([{'t': '2026-10-28T04:00:00Z', 'c': 10.0},
                                          {'t': 'bad'}])
    assert latest == 10.0 and prev is None
    prev, latest = H._resolve_prev_close([{'c': None}])
    assert (prev, latest) == (None, None)
