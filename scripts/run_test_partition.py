from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.partition_tests import (
    DEFAULT_TESTS_ROOT,
    _default_weights,
    _load_weights,
    discover_test_files,
    partition_paths,
    select_partition,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one measured pytest partition.")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--weights", type=Path)
    parser.add_argument("--tests-root", type=Path, default=DEFAULT_TESTS_ROOT)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    root = args.tests_root.resolve()
    files = discover_test_files(root, functional_only=True)
    print(f"Discovered {len(files)} tracked test files", flush=True)
    weights = _default_weights(files, root)
    weights.update(_load_weights(args.weights))
    shards = partition_paths(files, args.count, weights)
    flattened = [path for shard in shards for path in shard]
    if len(flattened) != len(set(flattened)) or set(flattened) != set(files):
        raise SystemExit("partition inventory is incomplete or overlapping")
    selected = select_partition(shards, args.index)
    if not selected:
        raise SystemExit(f"partition {args.index} contains no test files")
    prefix = "tests" if root == DEFAULT_TESTS_ROOT else root.name
    paths = [f"{prefix}/{relative}".replace("\\", "/") for relative in selected]
    print(f"Partition {args.index}/{args.count}: {len(paths)} test files", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args, *paths],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
