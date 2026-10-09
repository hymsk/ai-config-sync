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


def prune_backups(backup_root: Path, repo_path: str, keep: int):
    """按 run_id 排序删除同一 repo_path 的旧备份，只保留最近 keep 份。

    keep <= 0 表示不清理。run_id 字典序即近似时间序（时间戳格式单调）；
    非时间戳 run_id（如 adopt-repo）排在时间戳之后，天然保留更久。
    空的 run_id 目录顺手移除。
    """
    if keep <= 0:
        return
    root = Path(backup_root)
    if not root.is_dir():
        return
    versions = []
    for run_dir in root.iterdir():
        if run_dir.is_dir() and (run_dir / repo_path).is_file():
            versions.append(run_dir)
    versions.sort()
    for run_dir in versions[:-keep] if len(versions) > keep else []:
        target = run_dir / repo_path
        try:
            target.unlink()
            # 仅清理 repo_path 祖先链上 run_dir 以内的空目录，不越界
            node = target.parent
            while node != run_dir:
                node.rmdir()
                node = node.parent
            run_dir.rmdir()
        except OSError:
            pass  # 目录非空或清理失败不影响同步；文件已删，下轮再试


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
