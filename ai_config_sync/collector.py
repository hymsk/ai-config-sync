"""本机文件采集：SHA-256 摘要与稳定读。"""

import hashlib
import time
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_stable(path: Path, delay: float = 0.05):
    """两次读取摘要一致才视为稳定，返回 (bytes, hash)；否则 None。

    摘要不一致或读取失败都视为"正在写入/不可读"，交给下一轮。
    """
    try:
        first = path.read_bytes()
        digest = sha256_bytes(first)
        time.sleep(delay)
        second = path.read_bytes()
        if sha256_bytes(second) == digest:
            return first, digest
    except OSError:
        return None
    return None
