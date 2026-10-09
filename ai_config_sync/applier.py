"""写回本机：内容校验、备份、原子替换（保留 mode/owner）。"""

import json
import os
import shutil
from pathlib import Path

from . import tomlite


class ApplyError(Exception):
    pass


def validate(path: Path, data: bytes):
    """按扩展名做可解析性校验；失败抛 ApplyError。其他格式默认放行。"""
    name = Path(path).name.lower()
    try:
        if name.endswith(".json"):
            json.loads(data.decode("utf-8"))
        elif name.endswith(".toml"):
            tomlite.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, tomlite.TomlError) as exc:
        raise ApplyError("内容校验失败 {}: {}".format(path, exc))


def backup(path: Path, backup_root: Path, run_id: str, repo_path: str):
    if not Path(path).exists():
        return None
    dest = Path(backup_root) / run_id / repo_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(path), str(dest))
    return dest


def atomic_write(path: Path, data: bytes):
    """同目录临时文件 + fsync + rename；保留既有 mode/uid/gid。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old_stat = path.stat() if path.exists() else None
    mode = (old_stat.st_mode & 0o777) if old_stat else 0o644
    tmp = path.with_name(path.name + ".aics-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(str(tmp), mode)
    if old_stat is not None:
        try:
            os.chown(str(tmp), old_stat.st_uid, old_stat.st_gid)
        except (PermissionError, OSError):
            pass
    os.replace(str(tmp), str(path))
    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
