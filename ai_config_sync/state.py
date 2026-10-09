"""追加式事务日志：唯一本机状态。

每阶段先 fsync 再执行动作；崩溃后按最后一条事务记录恢复。
损坏时不猜测：由调用方进入 paused-needs-doctor。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

RESUMABLE = ("captured", "merged", "pushed", "applied")


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, phase, **fields):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "phase": phase}
        entry.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _entries(self):
        """有效记录列表；文件损坏（任一行非法 JSON）返回 None。"""
        if not self.path.is_file():
            return []
        out = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                return None
        return out

    def last(self):
        entries = self._entries()
        if entries is None:
            return "corrupt"
        return entries[-1] if entries else None

    def resumable(self):
        """最近的可恢复事务记录。

        自后向前：遇到 conflict（含其后未完成的人工流程）→ 不可恢复，
        交由冲突处理路径；遇到 checkpointed/round-begin → 全新轮次。
        """
        entries = self._entries()
        if entries is None:
            return "corrupt"
        for entry in reversed(entries):
            phase = entry.get("phase")
            if phase == "conflict":
                return None
            if phase in RESUMABLE:
                return entry
            if phase in ("checkpointed", "round-begin"):
                return None
        return None

    def rotate(self, keep=100):
        entries = self._entries()
        if entries is None or len(entries) <= keep * 2:
            return
        tail = entries[-keep:]
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in tail:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(self.path))
