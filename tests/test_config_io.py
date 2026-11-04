"""配置读写的 P0 回归：原子写 + 坏配置留证，避免自选股被静默覆写丢失"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import floating_window as fw


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / 'watchlist_config.json'
    monkeypatch.setattr(fw, 'CONFIG_FILE', str(path))
    return str(path)


def test_save_config_roundtrip(cfg_file):
    assert fw.save_config({'interval': 3, 'alerts': [{'code': 'sh601398', 'name': '工商银行'}]})
    with open(cfg_file, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    assert data['alerts'][0]['name'] == '工商银行'
    assert not os.path.exists(cfg_file + '.tmp')


def test_save_config_keeps_original_when_write_fails(cfg_file, monkeypatch):
    """写入中途失败（原实现会留下半截文件，下次启动即解析失败）：
    真配置必须保持原样，临时文件必须清掉"""
    original = {'interval': 5, 'alerts': [{'code': 'sh600000'}]}
    with open(cfg_file, 'w', encoding='utf-8-sig') as f:
        json.dump(original, f)

    def boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(json, 'dump', boom)
    assert fw.save_config({'interval': 9, 'alerts': []}) is False
    assert not os.path.exists(cfg_file + '.tmp')
    with open(cfg_file, 'r', encoding='utf-8-sig') as f:
        assert json.load(f) == original


def test_corrupt_config_is_backed_up_not_eaten(cfg_file):
    """坏配置改名留证：否则回退的空 alerts 会被下一次保存覆写上去，自选股不可恢复"""
    with open(cfg_file, 'w', encoding='utf-8-sig') as f:
        f.write('{"interval": 3, "alerts": [{"code": "sh601398"')   # 截断的 JSON

    config = fw.load_config()
    assert config['alerts'] == []
    assert not os.path.exists(cfg_file)                # 原文件已让位给备份
    backups = [n for n in os.listdir(os.path.dirname(cfg_file))
               if n.startswith('watchlist_config.json.broken-')]
    assert len(backups) == 1
    with open(os.path.join(os.path.dirname(cfg_file), backups[0]), encoding='utf-8-sig') as f:
        assert 'sh601398' in f.read()                  # 原内容仍可手工找回


def test_normalize_config_repairs_scalars_and_skips_bad_items():
    raw = {
        'interval': 0.5,
        'source': 'yahoo',
        'theme': 'neon',
        'kline_size': 'huge',
        'tdx_nodes': [
            {'host': ''},
            {'host': '1.2.3.4:7709', 'name': '测试节点', 'priority': 'bad'},
        ],
        'alerts': [
            {'code': 'SH601398', 'name': '工商银行', 'target': '6.5', 'dir': 'oops'},
            {'code': 'bj430047', 'name': '北交所'},
            {'code': 'sh600000', 'target': 0},
            {'code': 'sh600001', 'target_pct': 0},
            {'code': 'sz000001', 'target_pct': -5, 'pct_dir': 'above'},
            'not-a-dict',
        ],
    }

    config = fw.normalize_config(raw)

    assert config['interval'] == 1
    assert config['source'] == 'auto'
    assert config['theme'] == 'auto'
    assert config['kline_size'] == 'small'
    assert config['tdx_nodes'] == [{
        'id': '1.2.3.4:7709', 'name': '测试节点',
        'host': '1.2.3.4:7709', 'priority': 99,
    }]
    assert [item['code'] for item in config['alerts']] == ['sh601398', 'sz000001']
    assert config['alerts'][0]['target'] == 6.5
    assert config['alerts'][0]['dir'] == 'below'       # 非法方向回退默认
    assert config['alerts'][1]['target_pct'] == 5      # 负数按绝对值保存


def test_load_config_keeps_file_when_only_some_alerts_are_invalid(cfg_file):
    """局部坏项只跳过该行，不能触发整份配置备份/清空。"""
    data = {'interval': 5, 'alerts': [
        {'code': 'bj430047', 'name': '北交所'},
        {'code': 'SH601398', 'name': '工商银行', 'target': 6.0},
    ]}
    with open(cfg_file, 'w', encoding='utf-8-sig') as f:
        json.dump(data, f)

    config = fw.load_config()

    assert [item['code'] for item in config['alerts']] == ['sh601398']
    assert os.path.exists(cfg_file)
    backups = [n for n in os.listdir(os.path.dirname(cfg_file))
               if n.startswith('watchlist_config.json.broken-')]
    assert backups == []


def test_save_config_rejects_non_object_without_touching_file(cfg_file):
    assert fw.save_config(['not', 'a', 'config']) is False
    assert not os.path.exists(cfg_file)
