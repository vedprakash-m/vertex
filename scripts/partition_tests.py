from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTS_ROOT = REPO_ROOT / "tests"


def discover_test_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("test_*.py")
        if "__pycache__" not in path.parts
    )


def partition_paths(
    paths: Sequence[str],
    shard_count: int,
    weights: Mapping[str, float],
) -> list[list[str]]:
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    shards: list[list[str]] = [[] for _ in range(shard_count)]
    totals = [0.0] * shard_count
    for path in sorted(set(paths), key=lambda item: (-weights.get(item, 1.0), item)):
        index = min(range(shard_count), key=lambda candidate: (totals[candidate], candidate))
        shards[index].append(path)
        totals[index] += weights.get(path, 1.0)
    return [sorted(shard) for shard in shards]


def select_partition(shards: Sequence[Sequence[str]], index: int) -> list[str]:
    if index < 0 or index >= len(shards):
        raise ValueError(f"shard index {index} is outside 0..{len(shards) - 1}")
    return list(shards[index])


def _load_weights(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files") if isinstance(payload, dict) else payload
    if not isinstance(files, dict):
        raise ValueError("weight file must contain an object or a 'files' object")
    result: dict[str, float] = {}
    for name, value in files.items():
        if isinstance(value, dict):
            value = value.get("seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"invalid test weight for {name}: {value!r}")
        result[str(name)] = float(value)
    return result


def _default_weights(files: Sequence[str], root: Path) -> dict[str, float]:
    result: dict[str, float] = {}
    for relative in files:
        path = root / relative
        text = path.read_text(encoding="utf-8", errors="ignore")
        test_count = text.count("\ndef test_") + text.count("\n    def test_")
        result[relative] = max(1.0, len(text.splitlines()) + test_count * 20)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Partition pytest files deterministically.")
    parser.add_argument("--tests-root", type=Path, default=DEFAULT_TESTS_ROOT)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--nul", action="store_true")
    args = parser.parse_args(argv)

    root = args.tests_root.resolve()
    files = discover_test_files(root)
    weights = _default_weights(files, root)
    weights.update(_load_weights(args.weights))
    shards = partition_paths(files, args.count, weights)
    flattened = [path for shard in shards for path in shard]
    if args.verify and (len(flattened) != len(set(flattened)) or set(flattened) != set(files)):
        raise SystemExit("partition inventory is incomplete or overlapping")
    selected = select_partition(shards, args.index)
    prefix = "tests" if root == DEFAULT_TESTS_ROOT else root.name
    encoded = [
        f"{prefix}/{relative}".replace("\\", "/")
        for relative in selected
    ]
    if args.nul:
        os.write(1, b"".join((path + "\0").encode("utf-8") for path in encoded))
    else:
        for path in encoded:
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
