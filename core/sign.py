"""小黑盒 Web 接口签名。

两套签名，缺一不可：

* ``hkey``：所有请求 query 里的路径签名，由 ``路径 + _time + nonce`` 计算。
* ``_rnd``：网页写接口（发帖/评论/点赞等）额外要求的 Workshop v15 签名。

两者都来自前端 bundle 的公开算法，这里按算法重写，不依赖第三方包。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from itertools import zip_longest

#: 32 字符映射表
ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

#: Workshop v15 的固定盐
WORKSHOP_SALT = "Z7mFG4tQp9Ws2LxB8H"

_HEX = "0123456789ABCDEF"


# ---------------------------------------------------------------------------
# GF(2^8) 上的线性变换（AES 的 0x1B 约简）
# ---------------------------------------------------------------------------
def _double(v: int) -> int:
    return ((v << 1) ^ (0x1B if v & 0x80 else 0)) & 0xFF


def _three(v: int) -> int:
    return _double(v) ^ v


def _nine(v: int) -> int:
    return _three(_double(v))


def _eleven(v: int) -> int:
    return _nine(_three(_double(v)))


def _thirteen(v: int) -> int:
    return _eleven(v) ^ _nine(v) ^ _three(v)


def _map_chars(value: str, alphabet: str) -> str:
    """用字符码点对表长取模当下标。"""
    return "".join(alphabet[ord(ch) % len(alphabet)] for ch in value)


def normalize_path(path: str) -> str:
    """去 query、去空段、首尾补 ``/``。"""
    path = path.split("?", 1)[0]
    return "/" + "/".join(seg for seg in path.split("/") if seg) + "/"


def hkey(path: str, timestamp: int, nonce: str) -> str:
    """计算 ``hkey``（7 字符：5 位主干 + 2 位校验）。"""
    normalized = normalize_path(path)

    material = (
        _map_chars(str(timestamp), ALPHABET[:-2]),
        _map_chars(normalized, ALPHABET),
        _map_chars(nonce, ALPHABET),
    )
    seed = "".join("".join(chars) for chars in zip_longest(*material, fillvalue=""))[:20]
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()

    a, b, c, d = (ord(ch) for ch in digest[-6:][:4])
    mixed = (
        _thirteen(a) ^ _eleven(b) ^ _nine(c) ^ _three(d),
        _three(a) ^ _thirteen(b) ^ _eleven(c) ^ _nine(d),
        _nine(a) ^ _three(b) ^ _thirteen(c) ^ _eleven(d),
        _eleven(a) ^ _nine(b) ^ _three(c) ^ _thirteen(d),
    )
    # 原实现保留被覆盖前的最后两个字符一起参与校验位
    checksum = (sum(mixed) + sum(ord(ch) for ch in digest[-2:])) % 100

    return _map_chars(digest[:5], ALPHABET[:-4]) + f"{checksum:02d}"


def signed_params(path: str, timestamp: int | None = None, nonce: str | None = None) -> dict:
    """生成可直接并入 query 的 ``hkey`` / ``_time`` / ``nonce``。"""
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or "".join(secrets.choice(_HEX) for _ in range(32))
    return {
        "hkey": hkey(path, timestamp, nonce),
        "_time": timestamp,
        "nonce": nonce,
    }


def workshop_rnd(params: dict) -> str:
    """计算写接口的 ``_rnd``（形如 ``15:<hex>``）。

    公式：``HMAC-SHA256(salt, salt + nonce + _time + ":" + nonce)``。
    """
    nonce = str(params["nonce"])
    stamp = str(params["_time"])
    message = f"{WORKSHOP_SALT}{nonce}{stamp}:{nonce}"
    digest = hmac.new(
        WORKSHOP_SALT.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return "15:" + digest
