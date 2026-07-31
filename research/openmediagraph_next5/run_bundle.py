#!/usr/bin/env python3
"""Self-extracting OpenMediaGraph next-five research harness."""
from __future__ import annotations

import argparse
import base64
import io
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path


def extract() -> Path:
    bundle_root = Path(__file__).with_name("bundle")
    encoded = "".join(
        path.read_text(encoding="utf-8").strip()
        for path in sorted(bundle_root.glob("part*.txt"))
    )
    if not encoded:
        raise RuntimeError("research bundle payload is missing")
    payload = base64.b64decode(encoded)
    root = Path(tempfile.mkdtemp(prefix="openmediagraph-next5-"))
    resolved_root = root.resolve()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            destination = (root / member.name).resolve()
            if destination != resolved_root and resolved_root not in destination.parents:
                raise RuntimeError(f"unsafe bundled path: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"links are forbidden in bundle: {member.name}")
        archive.extractall(root, filter="data")
    return root


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    known, remaining = parser.parse_known_args()
    root = extract()
    sys.path.insert(0, str(root))
    if known.self_test:
        import unittest

        suite = unittest.defaultTestLoader.loadTestsFromName(
            "openmediagraph_next5.test_harness"
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    from openmediagraph_next5.runner import main as runner_main

    if "--output" in remaining:
        output = Path(remaining[remaining.index("--output") + 1])
        source_target = output / "SOURCE" / "openmediagraph_next5"
        source_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            root / "openmediagraph_next5", source_target, dirs_exist_ok=True
        )
    sys.argv = [sys.argv[0], *remaining]
    return runner_main()


if __name__ == "__main__":
    raise SystemExit(main())
