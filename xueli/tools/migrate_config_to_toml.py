from __future__ import annotations

import json
import sys
from pathlib import Path

from src.core.toml_utils import dumps_toml_document, prune_none_values


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    source_path = Path(argv[1]).resolve() if len(argv) > 1 else repo_root / "config.json"
    target_path = Path(argv[2]).resolve() if len(argv) > 2 else repo_root / "config.toml"

    if not source_path.exists():
        print(f"source config not found: {source_path}", file=sys.stderr)
        return 1

    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("source config must be a JSON object", file=sys.stderr)
        return 1

    cleaned = prune_none_values(payload)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(dumps_toml_document(cleaned), encoding="utf-8")
    print(f"migrated {source_path} -> {target_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
