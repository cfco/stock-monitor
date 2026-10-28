#!/usr/bin/env python3
"""
secret_store.py — 本地凭证加密存储（Windows DPAPI）

用途：
  Alpaca API Key/Secret 等敏感凭证不再明文存放于 watchlist_config.json，
  改为 DPAPI 加密后以 base64 密文存入 alpaca.api_key_enc / secret_enc 字段。

为什么选 DPAPI：
  - Windows 原生机制（crypt32.dll），ctypes 调用，零第三方依赖
  - 密钥绑定「当前 Windows 用户」：配置文件即使被拷走或误提交到仓库，
    在别的机器/别的用户下也无法解密，泄露风险大幅降低
  - 换机器迁移配置后凭证不可解密属预期行为（重新输入一次即可）

非 Windows 平台（Ubuntu 等）DPAPI 不存在：
  降级为 base64 编码保存（明文等价），启动时会打印一次提示。
  本项目主部署环境是 Windows 11，降级路径仅为不让程序在 Linux 上崩溃。

配置迁移（migrate_config）：
  旧格式 alpaca.{api_key, secret}（明文）→ 加密写入 alpaca.{api_key_enc, secret_enc}
  并删除明文字段。迁移是幂等的：没有明文残留时什么都不做。
"""

import base64
import ctypes
import json
import sys
# 注意：不要 import ctypes.wintypes —— 它在本文件中未使用，且在部分
# Linux Python 版本上导入会抛异常，会导致打包产物一启动就崩

# 应用专属熵（optional entropy）：同一 Windows 用户下，其它程序拿到密文也无法解
_ENTROPY = b'stock-monitor/alpaca-credentials/v1'
_IS_WINDOWS = (sys.platform == 'win32')
_non_windows_warned = False


class _DATA_BLOB(ctypes.Structure):
    """CRYPT_DATA_BLOB：DPAPI 输入/输出缓冲区结构（ctypes 无现成定义，自定义）"""
    _fields_ = [('cbData', ctypes.c_size_t),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _make_blob(data):
    """bytes → _DATA_BLOB；返回 (blob, buf)，buf 必须由调用方保持引用防止 GC 回收"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def _free_blob(blob):
    """释放 DPAPI 输出的 LocalAlloc 缓冲区"""
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def encrypt(plain):
    """明文字符串 → base64 密文字符串；空输入原样返回 ''"""
    if not plain:
        return ''
    data = plain.encode('utf-8')
    if _IS_WINDOWS:
        in_blob, in_buf = _make_blob(data)          # in_buf 保引用防 GC
        ent_blob, ent_buf = _make_blob(_ENTROPY)
        out_blob = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
            None, None, 0x01,  # CRYPTPROTECT_UI_FORBIDDEN：不弹任何系统 UI
            ctypes.byref(out_blob))
        if not ok:
            raise OSError('DPAPI CryptProtectData 失败: %s' %
                          ctypes.GetLastError())
        try:
            raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _free_blob(out_blob)
        return base64.b64encode(raw).decode('ascii')

    # 非 Windows 降级：base64（非加密，仅保证可运行）。首次使用时提示一次。
    global _non_windows_warned
    if not _non_windows_warned:
        print('[SecretStore] 非 Windows 平台：DPAPI 不可用，凭证将以 base64 明文等价保存')
        _non_windows_warned = True
    return base64.b64encode(data).decode('ascii')


def decrypt(cipher_b64):
    """base64 密文 → 明文字符串；解密失败/为空返回 ''（不抛异常，调用方按无凭证降级）"""
    if not cipher_b64:
        return ''
    try:
        raw = base64.b64decode(cipher_b64)
    except Exception:
        print('[SecretStore] 密文 base64 解析失败，按无凭证处理')
        return ''
    if _IS_WINDOWS:
        in_blob, in_buf = _make_blob(raw)
        ent_blob, ent_buf = _make_blob(_ENTROPY)
        out_blob = _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
            0x01, ctypes.byref(out_blob))
        if not ok:
            # 常见于：配置从别的机器/用户拷贝过来（DPAPI 密钥不匹配）
            print('[SecretStore] DPAPI 解密失败（可能配置来自其它机器/用户），按无凭证处理')
            return ''
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode('utf-8')
        finally:
            _free_blob(out_blob)

    # 非 Windows 降级读取
    try:
        return raw.decode('utf-8')
    except Exception:
        return ''


def load_alpaca_creds(alc):
    """从 alpaca 配置段读取凭证，返回 (api_key, secret)

    优先读 _enc 密文字段；若只有旧明文字段（迁移前）也兼容返回，
    但正常流程下 migrate_config 已在启动时把明文清掉了。
    """
    key = decrypt(alc.get('api_key_enc') or '')
    secret = decrypt(alc.get('secret_enc') or '')
    if not key:
        key = alc.get('api_key') or ''      # 旧明文兜底（理论上不会走到）
    if not secret:
        secret = alc.get('secret') or ''
    return key, secret


def store_alpaca_creds(alc, api_key, secret):
    """加密写入凭证到 alpaca 配置段（就地修改 dict），并确保无明文残留"""
    alc['api_key_enc'] = encrypt(api_key or '')
    alc['secret_enc'] = encrypt(secret or '')
    alc.pop('api_key', None)
    alc.pop('secret', None)


def migrate_config(config):
    """把配置里的旧明文凭证迁移为 DPAPI 密文。

    返回 True 表示发生了变更（调用方应落盘保存）；False 表示无需迁移。
    幂等：重复调用不会重复加密（明文字段已被删除时直接跳过）。
    """
    alc = (config or {}).get('alpaca')
    if not isinstance(alc, dict):
        return False
    changed = False
    for plain_field, enc_field in (('api_key', 'api_key_enc'), ('secret', 'secret_enc')):
        plain = alc.get(plain_field)
        if plain:                                   # 有明文残留才迁移
            try:
                alc[enc_field] = encrypt(plain)
            except Exception as e:
                print(f'[SecretStore] {plain_field} 加密迁移失败: {e}（保留原字段，下次重试）')
                continue
            del alc[plain_field]                    # 迁移成功即删除明文
            changed = True
    if changed:
        print('[SecretStore] 已将明文凭证迁移为 DPAPI 密文')
    return changed


if __name__ == '__main__':
    # 简单自测：python scripts/secret_store.py
    demo = {'alpaca': {'enabled': True, 'api_key': 'K-DEMO', 'secret': 'S-DEMO'}}
    print('迁移:', migrate_config(demo))
    print('落盘形态:', json.dumps(demo))
    k, s = load_alpaca_creds(demo['alpaca'])
    print('解密往返:', 'OK' if (k == 'K-DEMO' and s == 'S-DEMO') else f'FAIL({k!r},{s!r})')
