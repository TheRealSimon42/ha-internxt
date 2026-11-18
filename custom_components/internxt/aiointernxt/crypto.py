"""Cryptographic primitives used by Internxt clients.

Ported from internxt/rclone-adapter (MIT): crypto/crypto.go and buckets/encrypt.go.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import (
    Cipher,
    CipherContext,
    algorithms,
    modes,
)

# Public constant shipped with every Internxt client (APP_CRYPTO_SECRET).
APP_CRYPTO_SECRET = "6KYQBP847D4ATSFA"  # noqa: S105 - public, in every client
_SALTED_PREFIX = b"Salted__"
_AES_BLOCK = 16


# --- OpenSSL "Salted__" text encryption (AES-256-CBC, MD5 key derivation) ---


def _key_iv_from(secret: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """Derive key and IV with three MD5 rounds (EVP_BytesToKey style)."""
    password = secret + salt
    hashes: list[bytes] = []
    digest = password
    for _ in range(3):
        h = hashlib.md5(digest).digest()  # noqa: S324 - protocol requirement
        hashes.append(h)
        digest = h + password
    return hashes[0] + hashes[1], hashes[2]


def encrypt_text(plain: str, secret: str = APP_CRYPTO_SECRET) -> str:
    """Encrypt text into OpenSSL-compatible hex (Salted__ + salt + ciphertext)."""
    salt = secrets.token_bytes(8)
    key, iv = _key_iv_from(secret.encode(), salt)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plain.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return (_SALTED_PREFIX + salt + enc.update(data) + enc.finalize()).hex()


def decrypt_text(encrypted_hex: str, secret: str = APP_CRYPTO_SECRET) -> str:
    """Decrypt OpenSSL-compatible hex produced by encrypt_text or Internxt."""
    raw = bytes.fromhex(encrypted_hex)
    if len(raw) < 16 or raw[:8] != _SALTED_PREFIX:
        raise ValueError("invalid OpenSSL format: missing Salted__ prefix")
    body = raw[16:]
    if len(body) % _AES_BLOCK:
        raise ValueError("ciphertext is not a multiple of block size")
    key, iv = _key_iv_from(secret.encode(), raw[8:16])
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    data = unpadder.update(dec.update(body) + dec.finalize()) + unpadder.finalize()
    return data.decode()


def encrypt_password_hash(password: str, encrypted_salt: str) -> str:
    """Build the password field for the login access request."""
    salt_hex = decrypt_text(encrypted_salt)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha1", password.encode(), bytes.fromhex(salt_hex), 10000, 32
    )
    return encrypt_text(pw_hash.hex())


# --- File encryption (AES-256-CTR, key derived from the BIP39 mnemonic) ---


def mnemonic_seed(mnemonic: str) -> bytes:
    """BIP39 seed without passphrase."""
    return hashlib.pbkdf2_hmac("sha512", mnemonic.encode(), b"mnemonic", 2048, 64)


def bucket_key(mnemonic: str, bucket_id: str) -> bytes:
    """SHA512(seed || bucketId bytes)."""
    return hashlib.sha512(mnemonic_seed(mnemonic) + bytes.fromhex(bucket_id)).digest()


def file_key_iv(mnemonic: str, bucket_id: str, index: bytes) -> tuple[bytes, bytes]:
    """Per-file AES key and IV from mnemonic, bucket and the random 32-byte index."""
    return file_key_iv_from_bucket_key(bucket_key(mnemonic, bucket_id), index)


def file_key_iv_from_bucket_key(bkey: bytes, index: bytes) -> tuple[bytes, bytes]:
    """Same as file_key_iv with a precomputed bucket key (PBKDF2 is the slow part)."""
    return hashlib.sha512(bkey[:32] + index).digest()[:32], index[:16]


def ctr_cipher_at(key: bytes, iv: bytes, offset: int = 0) -> CipherContext:
    """AES-256-CTR context positioned at a byte offset of the stream."""
    counter = (int.from_bytes(iv, "big") + offset // _AES_BLOCK) % (1 << 128)
    ctx = Cipher(
        algorithms.AES(key), modes.CTR(counter.to_bytes(16, "big"))
    ).encryptor()
    if skip := offset % _AES_BLOCK:
        ctx.update(b"\0" * skip)
    return ctx


def file_hash(sha256_digest: bytes) -> str:
    """RIPEMD160(SHA256(ciphertext)) as hex, from the finished SHA-256 digest."""
    return ripemd160(sha256_digest).hex()


def bridge_auth_header(bridge_user: str, user_id: str) -> str:
    """Basic auth for the network (bucket) API."""
    password = hashlib.sha256(user_id.encode()).hexdigest()
    return "Basic " + base64.b64encode(f"{bridge_user}:{password}".encode()).decode()


# --- RIPEMD-160 (OpenSSL 3 may not ship it in the default provider) ---


def ripemd160(data: bytes) -> bytes:
    """RIPEMD-160 digest, using hashlib when available."""
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:
        return _ripemd160_py(data)


_R1 = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
]  # fmt: skip
_R2 = [
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
]  # fmt: skip
_S1 = [
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
]  # fmt: skip
_S2 = [
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
]  # fmt: skip
_K1 = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_K2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
_M32 = 0xFFFFFFFF


def _f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z)
    if j < 48:
        return (x | ~y) ^ z
    if j < 64:
        return (x & z) | (y & ~z)
    return x ^ (y | ~z)


def _rol(x: int, n: int) -> int:
    x &= _M32
    return ((x << n) | (x >> (32 - n))) & _M32


def _ripemd160_py(data: bytes) -> bytes:
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = (
        data
        + b"\x80"
        + b"\0" * ((55 - len(data)) % 64)
        + (len(data) * 8).to_bytes(8, "little")
    )
    for off in range(0, len(msg), 64):
        x = [
            int.from_bytes(msg[off + 4 * i : off + 4 * i + 4], "little")
            for i in range(16)
        ]
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = _rol(al + _f(j, bl, cl, dl) + x[_R1[j]] + _K1[j // 16], _S1[j]) + el
            al, el, dl, cl, bl = el, dl, _rol(cl, 10), bl, t & _M32
            t = (
                _rol(ar + _f(79 - j, br, cr, dr) + x[_R2[j]] + _K2[j // 16], _S2[j])
                + er
            )
            ar, er, dr, cr, br = er, dr, _rol(cr, 10), br, t & _M32
        t = (h[1] + cl + dr) & _M32
        h[1] = (h[2] + dl + er) & _M32
        h[2] = (h[3] + el + ar) & _M32
        h[3] = (h[4] + al + br) & _M32
        h[4] = (h[0] + bl + cr) & _M32
        h[0] = t
    return b"".join(v.to_bytes(4, "little") for v in h)
