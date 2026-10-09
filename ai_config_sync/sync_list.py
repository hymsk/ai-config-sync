"""sync.list 解析、匹配与文件系统展开。

语义：类 .gitignore 但默认不管理任何文件；普通行=纳入，!=排除，
后规则胜出；仅支持绝对路径；** 跨目录段，* 不跨 /。
"""


import os
import re
from dataclasses import dataclass

# 不允许展开遍历的根（含自身及子树）
FORBIDDEN_ROOTS = ("/", "/proc", "/sys", "/dev", "/run")


class SyncListError(Exception):
    pass


@dataclass
class Rule:
    negated: bool
    pattern: str
    regex: object


def _seg_regex(seg: str) -> str:
    out = []
    for ch in seg:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _compile(pattern: str):
    segs = [s for s in pattern.strip("/").split("/") if s]
    if not segs:
        raise SyncListError("空规则")
    rx = ""
    for idx, seg in enumerate(segs):
        last = idx == len(segs) - 1
        if seg == "**":
            rx += ".*" if last else "(?:[^/]+/)*"
        else:
            rx += _seg_regex(seg)
            if not last:
                rx += "/"
    # 末段为字面量时按目录语义匹配整个子树
    if segs[-1] != "**" and not any(c in segs[-1] for c in "*?"):
        rx += "(?:/.*)?"
    return re.compile("^" + rx + "$")


def parse(text: str):
    rules = []
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        negated = s.startswith("!")
        if negated:
            s = s[1:].strip()
        if not s:
            raise SyncListError("sync.list 第 {} 行: 空规则".format(lineno))
        if not s.startswith("/"):
            raise SyncListError(
                "sync.list 第 {} 行: 仅支持绝对路径规则: {}".format(lineno, s))
        if s == "/":
            raise SyncListError(
                "sync.list 第 {} 行: 不允许遍历根目录 /".format(lineno))
        try:
            regex = _compile(s)
        except re.error:
            raise SyncListError(
                "sync.list 第 {} 行: 规则无法编译: {}".format(lineno, s))
        rules.append(Rule(negated, s.rstrip("/"), regex))
    return rules


def match_path(rules, path: str) -> bool:
    """后规则胜出：返回最后一条命中规则的纳入状态；无命中=不管理。"""
    included = None
    rel = path.lstrip("/")
    for rule in rules:
        if rule.regex.match(rel):
            included = not rule.negated
    return bool(included)


def _literal_prefix(pattern: str) -> str:
    segs = pattern.strip("/").split("/")
    acc = []
    for seg in segs:
        if seg == "**" or "*" in seg or "?" in seg:
            break
        acc.append(seg)
    return "/" + "/".join(acc)


def _under(path: str, prefix: str) -> bool:
    """path 是否位于 prefix 内（含等于）。

    注意 "/" 前缀只匹配其自身：遍历禁区检查用 `_under(prefix, root)`
    判断"展开根是否落在禁区内"，规则 /tmp/x/** 的展开根是 /tmp/x，
    并不会因为一切路径都在 / 之下而被拒绝。
    """
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def in_zones(path: str, hard_zones) -> bool:
    return any(_under(path, str(z)) for z in hard_zones)


class Expanded:
    def __init__(self):
        self.files = {}      # host 绝对路径 -> 仓库相对路径 source/...
        self.warnings = []

    def add(self, host_path: str):
        self.files[host_path] = "source" + host_path


def _walk(prefix: str, hard_zones, warn):
    candidates = []
    for dirpath, dirnames, filenames in os.walk(prefix, followlinks=False):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d == ".git":
                continue
            if in_zones(full, hard_zones):
                warn("跳过硬排除目录: {}".format(full))
                continue
            if any(_under(full, r) for r in FORBIDDEN_ROOTS):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            candidates.append(os.path.join(dirpath, f))
    return candidates


def expand(rules, hard_zones, log=None):
    """展开清单为受管普通文件集合。

    - 规则字面前缀落在硬排除区或遍历禁区 -> SyncListError（配置错误）；
    - 宽规则展开时命中的硬排除子树 -> 剔除并告警；
    - 只收普通文件；软链接与非常规文件跳过并告警。
    """
    exp = Expanded()

    def warn(msg):
        exp.warnings.append(msg)
        if log:
            log(msg)

    seen_prefixes = set()
    for rule in rules:
        if rule.negated:
            continue
        segs = rule.pattern.strip("/").split("/")
        if ".git" in segs:
            raise SyncListError("sync.list 规则指向 .git: {}".format(rule.pattern))
        prefix = _literal_prefix(rule.pattern)
        for root in FORBIDDEN_ROOTS:
            if _under(prefix, root):
                raise SyncListError(
                    "sync.list 规则指向遍历禁区: {}".format(rule.pattern))
        for zone in hard_zones:
            if _under(prefix, str(zone)):
                raise SyncListError(
                    "sync.list 规则指向 Agent 自身目录: {}".format(rule.pattern))
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        has_wildcard = any(c in rule.pattern for c in "*?")
        if not has_wildcard:
            if os.path.isfile(prefix):
                candidates = [prefix]
            elif os.path.isdir(prefix):
                candidates = _walk(prefix, hard_zones, warn)
            else:
                candidates = []
        else:
            if not os.path.isdir(prefix):
                continue
            candidates = _walk(prefix, hard_zones, warn)
        for cand in candidates:
            if in_zones(cand, hard_zones):
                warn("跳过硬排除路径: {}".format(cand))
                continue
            if any(_under(cand, r) for r in FORBIDDEN_ROOTS):
                continue
            if os.path.islink(cand):
                warn("跳过软链接: {}".format(cand))
                continue
            if not os.path.isfile(cand):
                continue
            if match_path(rules, cand):
                exp.add(cand)
    return exp
