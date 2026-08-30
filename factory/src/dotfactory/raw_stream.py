"""Separate local capture for provider-native events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ledger import redact_payload


class RawEventStream:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def append(self, execution_id: str, attempt_id: str, event: dict[str, Any]) -> Path:
        directory = self.root / execution_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{attempt_id}.ndjson"
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(redact_payload(event), sort_keys=True) + "\n")
        return path
