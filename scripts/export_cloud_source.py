"""Export a verified, code-only snapshot of one currently deployed service.

This does not connect to a server, build an image, or deploy anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("web", "worker", "auth", "notifier")


def verified_sources(role: str, root: Path = ROOT):
    if role not in ROLES:
        raise ValueError("Unknown service role")
    manifest = json.loads((root / "cloud_sources" / f"{role}.json").read_text(encoding="utf-8"))
    sources = []
    for name, expected in manifest["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name:
            raise ValueError("Unsafe source path")
        source = root / "cloud_sources" / "overrides" / role / name
        if not source.is_file():
            source = root / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Snapshot has changed: {role}/{name}")
        sources.append((name, source))
    return sources


def export(role: str, destination: Path, root: Path = ROOT):
    sources = verified_sources(role, root)
    # Never overwrite an existing directory, including another exported snapshot.
    destination.mkdir(parents=True, exist_ok=False)
    for name, source in sources:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return len(sources)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args()
    if args.destination is None:
        count = len(verified_sources(args.role))
        print(f"{args.role}: {count} source hashes verified")
    else:
        count = export(args.role, args.destination)
        print(f"{args.role}: {count} verified source files exported")


if __name__ == "__main__":
    main()
