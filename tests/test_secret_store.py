"""非 Windows 凭证存储：拒绝明文等价持久化，且失败不能破坏原配置。"""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))

import secret_store as SS


@pytest.fixture
def non_windows(monkeypatch):
    monkeypatch.setattr(SS, '_IS_WINDOWS', False)


def test_non_windows_encrypt_refuses_plaintext_equivalent(non_windows):
    with pytest.raises(SS.SecretStoreError, match='Linux/macOS.*拒绝保存'):
        SS.encrypt('secret-value')


def test_non_windows_store_failure_leaves_config_unchanged(non_windows):
    alc = {'enabled': True, 'api_key': 'old-key', 'secret': 'old-secret'}
    before = dict(alc)
    with pytest.raises(SS.SecretStoreError):
        SS.store_alpaca_creds(alc, 'new-key', 'new-secret')
    assert alc == before
    assert 'api_key_enc' not in alc and 'secret_enc' not in alc


def test_windows_migration_is_atomic_when_second_encrypt_fails(monkeypatch):
    monkeypatch.setattr(SS, '_IS_WINDOWS', True)
    config = {'alpaca': {'api_key': 'old-key', 'secret': 'old-secret'}}
    before = {'api_key': 'old-key', 'secret': 'old-secret'}

    def encrypt(value):
        if value == 'old-secret':
            raise OSError('DPAPI unavailable')
        return 'encrypted-key'

    monkeypatch.setattr(SS, 'encrypt', encrypt)
    assert SS.migrate_config(config) is False
    assert config['alpaca'] == before


def test_non_windows_migration_removes_insecure_fields(non_windows):
    config = {'alpaca': {
        'enabled': True,
        'api_key': 'old-key', 'secret': 'old-secret',
        'api_key_enc': 'legacy-base64', 'secret_enc': 'legacy-base64',
    }}
    assert SS.migrate_config(config) is True
    assert config['alpaca'] == {'enabled': True}
    assert SS.load_alpaca_creds(config['alpaca']) == ('', '')


def test_non_windows_does_not_treat_legacy_base64_as_secure(non_windows):
    alc = {
        'api_key_enc': base64.b64encode(b'old-key').decode('ascii'),
        'secret_enc': base64.b64encode(b'old-secret').decode('ascii'),
    }
    assert SS.load_alpaca_creds(alc) == ('', '')
