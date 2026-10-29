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
