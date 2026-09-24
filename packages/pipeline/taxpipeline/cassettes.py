"""Record and replay API responses, so the eval runs offline.

A reviewer should be able to clone this repo and run `make eval` with no API
key and no network. A CI job should be able to gate on extraction quality
without spending money on every push.

So every live call is recorded to a cassette keyed by a hash of the request.
Replay is exact: same request, same response, byte for byte. If the request
changes - a different prompt, a different model, a different document - the key
changes and replay misses, which is the point. A cassette that silently served
a stale response for a changed prompt would make the eval a lie.

Cassettes are committed. They are the evidence that the numbers in
docs/EVALS.md came from somewhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = ["Cassette", "CassetteMiss"]

HERE = Path(__file__).parent / "cassettes"


class CassetteMiss(RuntimeError):
    """No recording for this request, and we are not allowed to make one."""


class Cassette:
    def __init__(self, directory: Path | None = None, *, record: bool = False) -> None:
        self.dir = directory or HERE
        self.record = record
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(request: dict[str, Any]) -> str:
        payload = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.blake2b(payload.encode(), digest_size=12).hexdigest()

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, request: dict[str, Any]) -> dict | None:
        p = self.path(self.key(request))
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        self.misses += 1
        return None

    def put(self, request: dict[str, Any], response: dict) -> None:
        key = self.key(request)
        self.path(key).write_text(json.dumps(response, indent=1, default=str))
        # A sidecar naming what the cassette is, so the directory is readable
        # rather than a wall of hashes.
        meta = {
            "key": key,
            "model": request.get("model"),
            "pass": request.get("_pass"),
            "document": request.get("_document"),
        }
        (self.dir / f"{key}.meta.json").write_text(json.dumps(meta, indent=1))

    def stats(self) -> str:
        total = self.hits + self.misses
        return f"{self.hits}/{total} served from cassette"
