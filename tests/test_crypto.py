"""加密工具测试（为天翼上传准备的地基）。

AES 用 FIPS-197 官方测试向量，RSA 用天翼实际下发的公钥结构。
这些算法不能"看起来对"，只能拿标准向量钉死。
"""
from __future__ import annotations

import unittest

from panmigrate.crypto import (
    aes_cbc_decrypt,
    aes_cbc_encrypt,
    aes_ecb_encrypt,
    hmac_sha1_hex,
    parse_rsa_public_key,
    pkcs7_pad,
    random_hex,
    random_uuid4,
    rsa_encrypt_pkcs1,
)

# FIPS-197 附录 C.1
FIPS_KEY = bytes(range(16))
FIPS_PT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CT = "69c4e0d86a7b0430d8cdb78070b4c55a"

TIANYI_PUB = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDC72L803mNmrQgyvaUt115S5gSHuDcS+nGdqBakHYq"
    "FShEwrEaqKsr2Z/7DQt9AobB0ne2vISUW0tXjhgf5vfl00kT7K+J4j+t3WLkQ6Zwc9KtZHkSW6/fkFSC"
    "1EnShPYLsG6rHYa5+wfefOY2P7yEFRsd5DGCqHNWkzOZclsXawIDAQAB"
)


class AesTest(unittest.TestCase):
    def test_fips197_vector(self):
        self.assertEqual(aes_ecb_encrypt(FIPS_KEY, FIPS_PT, pad=False).hex(), FIPS_CT)

    def test_pkcs7_padding_matches_block_size(self):
        self.assertEqual(pkcs7_pad(b"abc"), b"abc" + bytes([13]) * 13)
        self.assertEqual(pkcs7_pad(b"x" * 16), b"x" * 16 + bytes([16]) * 16)

    def test_encrypt_multiple_blocks(self):
        # 16 的整数倍也会补满整块（PKCS7 规则），所以 31 字节 -> 32 字节
        out = aes_ecb_encrypt(FIPS_KEY, b"a" * 31)
        self.assertEqual(len(out), 32)
        self.assertEqual(out[:16], aes_ecb_encrypt(FIPS_KEY, b"a" * 16, pad=False))
        self.assertEqual(len(aes_ecb_encrypt(FIPS_KEY, b"a" * 32)), 48)

    def test_rejects_wrong_key_length(self):
        with self.assertRaises(ValueError):
            aes_ecb_encrypt(b"short", b"data")


class RsaTest(unittest.TestCase):
    def test_parses_tianyi_public_key(self):
        n, e = parse_rsa_public_key(TIANYI_PUB)
        self.assertEqual(n.bit_length(), 1024)
        self.assertEqual(e, 65537)

    def test_ciphertext_length_matches_modulus(self):
        enc = rsa_encrypt_pkcs1(TIANYI_PUB, b"0123456789abcdef")
        import base64
        self.assertEqual(len(base64.b64decode(enc)), 128)

    def test_too_long_plaintext_is_rejected(self):
        with self.assertRaises(ValueError):
            rsa_encrypt_pkcs1(TIANYI_PUB, b"x" * 200)


class HmacTest(unittest.TestCase):
    def test_hmac_sha1_hex(self):
        # RFC 2202 用例 2：key="Jefe"，data="what do ya want for nothing?"
        self.assertEqual(
            hmac_sha1_hex("what do ya want for nothing?", "Jefe"),
            "effcdf6ae5eb2fa2d27416d5f184df9c259a7c79",
        )


class RandomTest(unittest.TestCase):
    def test_uuid_shape(self):
        value = random_uuid4()
        self.assertEqual(len(value), 36)
        self.assertEqual(value[14], "4")
        self.assertEqual(len(random_hex(16)), 16)




class AesCbcTest(unittest.TestCase):
    """115 云盘的上传请求体用的是 AES-128-CBC，这里钉住加解密往返。"""

    KEY = bytes.fromhex("fb1a19d652f5aaf7bc651d0f69bf422f")
    IV = bytes.fromhex("69bf422f49960550a0ad44ec3446cb4c")

    def test_roundtrip_various_lengths(self):
        for n in (0, 1, 15, 16, 17, 31, 32, 100, 1000):
            payload = bytes(range(256)) * (n // 256 + 1)
            payload = payload[:n]
            cipher = aes_cbc_encrypt(self.KEY, self.IV, payload)
            self.assertEqual(len(cipher) % 16, 0, f"{n}B 密文长度必须是 16 的倍数")
            self.assertEqual(aes_cbc_decrypt(self.KEY, self.IV, cipher), payload, f"{n}B 往返失败")

    def test_encrypt_is_deterministic_with_fixed_iv(self):
        msg = b"panmigrate 115 upload payload"
        a = aes_cbc_encrypt(self.KEY, self.IV, msg)
        b = aes_cbc_encrypt(self.KEY, self.IV, msg)
        self.assertEqual(a, b, "固定 key/iv 下必须可复现，否则服务端解不开")

    def test_different_iv_gives_different_cipher(self):
        msg = b"same content"
        iv2 = bytes(16)
        self.assertNotEqual(
            aes_cbc_encrypt(self.KEY, self.IV, msg),
            aes_cbc_encrypt(self.KEY, iv2, msg),
        )

    def test_bad_iv_length_rejected(self):
        with self.assertRaises(ValueError):
            aes_cbc_encrypt(self.KEY, b"short", b"x")

    def test_known_vector_padding(self):
        # 空输入的 PKCS7 会补满一个块
        cipher = aes_cbc_encrypt(self.KEY, self.IV, b"")
        self.assertEqual(len(cipher), 16)
        self.assertEqual(aes_cbc_decrypt(self.KEY, self.IV, cipher), b"")


if __name__ == "__main__":
    unittest.main()
