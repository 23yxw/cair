"""JSONL trace recorder. One event per line; images are referenced by id only."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


class TraceRecorder:
    def __init__(self, path: Optional[Path], trace_id: Optional[str] = None):
        self.trace_id = trace_id or uuid.uuid4().hex[:12]
        self.path = Path(path) if path else None
        self.events: List[Dict[str, Any]] = []
        self._t0 = time.time()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("w", encoding="utf-8")
        else:
            self._fh = None

    def log(self, type_: str, **data: Any) -> Dict[str, Any]:
        ev = {"ts": round(time.time() - self._t0, 3), "trace_id": self.trace_id, "type": type_, **data}
        self.events.append(ev)
        if self._fh:
            self._fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        return ev

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


def read_trace(path: Path) -> List[Dict[str, Any]]:
    out = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
