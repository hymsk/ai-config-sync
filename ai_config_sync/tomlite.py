"""极简 TOML 子集解析器。

部署环境可能只有 Python 3.9（无标准库 tomllib），而本工具的配置格式
受控（bootstrap.toml 与 .sync-config/config.toml），因此自带一个仅支持
所需语法的解析器：[section]、key = value、字符串/整数/浮点/布尔、# 注释。
不引入外部依赖。
"""


class TomlError(Exception):
    pass


def _strip_comment(raw: str) -> str:
    in_str = None
    for i, ch in enumerate(raw):
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
        elif ch == "#":
            return raw[:i]
    return raw


def _parse_value(raw: str):
    s = raw.strip()
    if not s:
        raise TomlError("空值")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    if s == "true":
        return True
    if s == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    raise TomlError("无法解析的值: {!r}".format(raw))


def loads(text: str) -> dict:
    data = {}
    current = data
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if not stripped.endswith("]"):
                raise TomlError("第 {} 行: 节标题格式错误".format(lineno))
            section = stripped[1:-1].strip()
            if not section:
                raise TomlError("第 {} 行: 空节名".format(lineno))
            node = data
            for part in section.split("."):
                part = part.strip().strip('"').strip("'")
                if not part:
                    raise TomlError("第 {} 行: 节名格式错误".format(lineno))
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise TomlError(
                        "第 {} 行: 节冲突: {}".format(lineno, section))
            current = node
            continue
        if "=" not in stripped:
            raise TomlError("第 {} 行: 缺少 '='".format(lineno))
        key, _, raw = stripped.partition("=")
        key = key.strip().strip('"').strip("'")
        if not key:
            raise TomlError("第 {} 行: 空键名".format(lineno))
        try:
            current[key] = _parse_value(_strip_comment(raw))
        except TomlError as exc:
            raise TomlError("第 {} 行: {}".format(lineno, exc))
    return data
