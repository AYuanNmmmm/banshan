"""纯标准库加密小工具。

天翼云盘的上传接口要求三件事，而这个项目坚持不引入任何第三方依赖，
所以自己实现：

  1. AES-128-ECB + PKCS7 —— 请求参数要加密后放在 `params` 里（十六进制）
  2. RSA PKCS#1 v1.5 加密 —— 会话密钥要用服务端下发的公钥加密，放在 EncryptionText 头
  3. HMAC-SHA1 —— 标准库 hmac 就行

AES 只做「加密 + ECB + PKCS7」这一条路径，够用且好验证（FIPS-197 有官方测试向量）。
RSA 只做「用公钥加密」，也就是幂运算 + PKCS#1 v1.5 填充，不需要私钥那一套。
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import os
import struct


# ---------------- AES ----------------
def _build_sbox() -> list[int]:
    """按定义生成 S 盒，避免手抄 256 个常量抄错。"""
    sbox = [0] * 256
    p = q = 1
    while True:
        # p 乘 3，q 除 3（GF(2^8) 上的运算）
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q
        for shift in (1, 2, 3, 4):
            x ^= ((q << shift) | (q >> (8 - shift))) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a & 0xFF


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("只支持 AES-128（16 字节密钥）")
    words = [list(key[i * 4: i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[i * 4: i * 4 + 4], []) for i in range(11)]


def _add_round_key(state: list[int], key: list[int]) -> list[int]:
    return [s ^ k for s, k in zip(state, key)]


def _sub_bytes(state: list[int]) -> list[int]:
    return [SBOX[b] for b in state]


def _shift_rows(s: list[int]) -> list[int]:
    # 状态按列存放：s[c*4 + r]
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = s[((c + r) % 4) * 4 + r]
    return out


def _mix_columns(s: list[int]) -> list[int]:
    out = []
    for c in range(4):
        a = s[c * 4: c * 4 + 4]
        t = a[0] ^ a[1] ^ a[2] ^ a[3]
        out.append(a[0] ^ t ^ _xtime(a[0] ^ a[1]))
        out.append(a[1] ^ t ^ _xtime(a[1] ^ a[2]))
        out.append(a[2] ^ t ^ _xtime(a[2] ^ a[3]))
        out.append(a[3] ^ t ^ _xtime(a[3] ^ a[0]))
    return out


def _encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = _add_round_key(list(block), round_keys[0])
    for rnd in range(1, 10):
        state = _sub_bytes(state)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, round_keys[rnd])
    state = _sub_bytes(state)
    state = _shift_rows(state)
    state = _add_round_key(state, round_keys[10])
    return bytes(state)


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def aes_ecb_encrypt(key: bytes, data: bytes, pad: bool = True) -> bytes:
    """AES-128-ECB 加密（默认 PKCS7 填充）。"""
    if pad:
        data = pkcs7_pad(data)
    if len(data) % 16:
        raise ValueError("待加密数据长度必须是 16 的倍数")
    round_keys = _expand_key(key)
    return b"".join(
        _encrypt_block(data[i: i + 16], round_keys) for i in range(0, len(data), 16)
    )


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 加密（PKCS7 填充）。

    115 网盘的上传请求体要求这种格式：密钥和 IV 都是固定常量 —— 名字里带 ECDH，
    实际并没有椭圆曲线运算，所以纯标准库就能算。
    """
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    padded = pkcs7_pad(data, 16)
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(padded), 16):
        block = bytes(a ^ b for a, b in zip(padded[i: i + 16], prev))
        enc = _encrypt_block(block, round_keys)
        out += enc
        prev = enc
    return bytes(out)



# ---------------- AES 解密（CBC 需要） ----------------
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i


def _gmul(a: int, b: int) -> int:
    """GF(2^8) 乘法，用于 InvMixColumns。"""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return result


def _inv_shift_rows(s: list[int]) -> list[int]:
    # 与加密相反：第 r 行右移 r
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = s[((c - r) % 4) * 4 + r]
    return out


def _inv_mix_columns(s: list[int]) -> list[int]:
    out = []
    for c in range(4):
        a = s[c * 4: c * 4 + 4]
        out.append(_gmul(a[0], 14) ^ _gmul(a[1], 11) ^ _gmul(a[2], 13) ^ _gmul(a[3], 9))
        out.append(_gmul(a[0], 9) ^ _gmul(a[1], 14) ^ _gmul(a[2], 11) ^ _gmul(a[3], 13))
        out.append(_gmul(a[0], 13) ^ _gmul(a[1], 9) ^ _gmul(a[2], 14) ^ _gmul(a[3], 11))
        out.append(_gmul(a[0], 11) ^ _gmul(a[1], 13) ^ _gmul(a[2], 9) ^ _gmul(a[3], 14))
    return out


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = _add_round_key(list(block), round_keys[10])
    for rnd in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[b] for b in state]
        state = _add_round_key(state, round_keys[rnd])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [INV_SBOX[b] for b in state]
    state = _add_round_key(state, round_keys[0])
    return bytes(state)


def _pkcs7_unpad(data: bytes, block: int = 16) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= block and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 解密（去 PKCS7 填充）。115 的上传响应就是这种格式。"""
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    round_keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data) - (len(data) % 16), 16):
        block = data[i: i + 16]
        plain = _decrypt_block(block, round_keys)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = block
    return _pkcs7_unpad(bytes(out))


# ---------------- RSA ----------------
def _der_read(buf: bytes, pos: int) -> tuple[int, int, bytes]:
    """读一个 DER TLV，返回 (tag, 内容长度, 内容)。"""
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos: pos + n], "big")
        pos += n
    return tag, length, buf[pos: pos + length], pos + length


def parse_rsa_public_key(b64_der: str) -> tuple[int, int]:
    """从 SubjectPublicKeyInfo（base64 DER）里取出 (n, e)。"""
    der = base64.b64decode(b64_der)
    _, _, seq, _ = _der_read(der, 0)          # SEQUENCE
    _, _, _alg, nxt = _der_read(seq, 0)       # AlgorithmIdentifier
    tag, _, bitstr, _ = _der_read(seq, nxt)   # BIT STRING
    if tag != 0x03:
        raise ValueError("公钥格式不对：没有 BIT STRING")
    inner = bitstr[1:]                        # 跳过 unused-bits 字节
    _, _, rsa_seq, _ = _der_read(inner, 0)    # SEQUENCE { INTEGER n, INTEGER e }
    _, _, n_bytes, pos = _der_read(rsa_seq, 0)
    _, _, e_bytes, _ = _der_read(rsa_seq, pos)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_encrypt_pkcs1(pubkey_b64_der: str, message: bytes) -> str:
    """RSA 公钥加密（PKCS#1 v1.5 type 2 填充），返回 base64 —— 对应前端 JSEncrypt。"""
    n, e = parse_rsa_public_key(pubkey_b64_der)
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 11:
        raise ValueError("明文太长，放不进这个 RSA 密钥")
    pad_len = k - len(message) - 3
    padding = bytearray()
    while len(padding) < pad_len:
        byte = os.urandom(1)[0]
        if byte:
            padding.append(byte)
    block = b"\x00\x02" + bytes(padding) + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(cipher.to_bytes(k, "big")).decode()


# ---------------- HMAC ----------------
def hmac_sha1_hex(message: str, key: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha1).hexdigest()


def random_uuid4() -> str:
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return (
        f"{b[0]:02x}{b[1]:02x}{b[2]:02x}{b[3]:02x}-{b[4]:02x}{b[5]:02x}-"
        f"{b[6]:02x}{b[7]:02x}-{b[8]:02x}{b[9]:02x}-"
        f"{b[10]:02x}{b[11]:02x}{b[12]:02x}{b[13]:02x}{b[14]:02x}{b[15]:02x}"
    )


def random_hex(length: int) -> str:
    return os.urandom((length + 1) // 2).hex()[:length]
