"""Crypto tests against independent references (OpenSSL CLI, BIP39 and RIPEMD-160 vectors)."""

import hashlib
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from aiointernxt import crypto

OPENSSL = shutil.which("openssl")


@pytest.mark.skipif(not OPENSSL, reason="openssl CLI not available")
def test_decrypt_text_matches_openssl_cli() -> None:
    out = subprocess.run(
        [OPENSSL, "enc", "-aes-256-cbc", "-md", "md5", "-S", "0102030405060708",
         "-pass", "pass:" + crypto.APP_CRYPTO_SECRET],
        input=b"hello internxt", capture_output=True, check=True,
    ).stdout  # fmt: skip
    # OpenSSL 3 omits the header when -S is given; add it like OpenSSL 1.x did.
    if not out.startswith(b"Salted__"):
        out = b"Salted__" + bytes.fromhex("0102030405060708") + out
    assert crypto.decrypt_text(out.hex()) == "hello internxt"


@pytest.mark.skipif(not OPENSSL, reason="openssl CLI not available")
def test_encrypt_text_readable_by_openssl_cli() -> None:
    enc = bytes.fromhex(crypto.encrypt_text("secret mnemonic words", "pw"))
    out = subprocess.run(
        [OPENSSL, "enc", "-d", "-aes-256-cbc", "-md", "md5", "-pass", "pass:pw"],
        input=enc, capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert out == b"secret mnemonic words"


def test_text_roundtrip_and_bad_prefix() -> None:
    assert crypto.decrypt_text(crypto.encrypt_text("äöü ß", "k"), "k") == "äöü ß"
    with pytest.raises(ValueError):
        crypto.decrypt_text("00" * 32)


def test_bip39_seed_reference_vector() -> None:
    mnemonic = " ".join(["abandon"] * 11 + ["about"])
    assert crypto.mnemonic_seed(mnemonic).hex() == (
        "5eb00bbddcf069084889a8ab9155568165f5c453ccb85e70811aaed6f6da5fc1"
        "9a5ac40b389cd370d086206dec8aa6c43daea6690f20ad3d8d48b2d2ce9e38e4"
    )


@pytest.mark.parametrize("data", [b"", b"abc", b"a" * 55, b"a" * 56, b"x" * 1000])
def test_ripemd160_python_fallback(data: bytes) -> None:
    assert crypto._ripemd160_py(data) == crypto.ripemd160(data)


def test_ripemd160_reference_vectors() -> None:
    assert crypto._ripemd160_py(b"").hex() == "9c1185a5c5e9fc54612808977ee8f548b2258d31"
    assert (
        crypto._ripemd160_py(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    )


def test_file_key_matches_adapter_formula() -> None:
    mnemonic = " ".join(["abandon"] * 11 + ["about"])
    bucket = "0123456789abcdef01234567"
    index = bytes(range(32))
    seed = hashlib.pbkdf2_hmac("sha512", mnemonic.encode(), b"mnemonic", 2048, 64)
    bkey = hashlib.sha512(seed + bytes.fromhex(bucket)).digest()
    expected = hashlib.sha512(bkey[:32] + index).digest()[:32]
    key, iv = crypto.file_key_iv(mnemonic, bucket, index)
    assert key == expected
    assert iv == index[:16]


@pytest.mark.parametrize("offset", [0, 1, 15, 16, 17, 1000, 4096 + 7])
def test_ctr_at_offset_equals_continuous_stream(offset: int) -> None:
    key, iv = (
        bytes(range(32)),
        bytes([0xFF] * 16),
    )  # IV overflow wraps like Go's big.Int + slice
    data = bytes(range(256)) * 20
    full = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor().update(data)
    assert crypto.ctr_cipher_at(key, iv, offset).update(data[offset:]) == full[offset:]


def test_bridge_auth_header() -> None:
    header = crypto.bridge_auth_header("user@example.com", "abc")
    import base64

    user, pw = base64.b64decode(header.removeprefix("Basic ")).decode().split(":")
    assert user == "user@example.com"
    assert pw == hashlib.sha256(b"abc").hexdigest()
