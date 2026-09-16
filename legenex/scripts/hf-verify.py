#!/usr/bin/env python3
"""Verify a local model directory against a pinned Hugging Face revision.

Every file the revision publishes (optionally filtered) must exist locally
with the published size; LFS files must also match the published sha256.
Writes `.gx-manifest.json` into the directory on success, recording the
repository, the exact revision and every verified file, so later tooling
(Model Manager, audits) never has to trust a moving branch.

Usage:
    hf-verify.py REPO REVISION DIR [--include GLOB ...] [--exclude GLOB ...]
                 [--jobs N] [--no-hash] [--token-file PATH]
    hf-verify.py REPO REVISION - --file REPO_PATH=LOCAL_PATH [...] --manifest OUT.json

The second form verifies files that were moved out of the repository layout
(e.g. into ComfyUI model folders) and writes the manifest to OUT.json.

Exit status: 0 verified, 1 mismatch/missing, 2 usage or API error.
Standard library only.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://huggingface.co/api/models/{repo}/revision/{rev}?blobs=true"


def _token(path: str | None) -> str | None:
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None
    return os.environ.get("HF_TOKEN") or None


def fetch_listing(repo: str, rev: str, token: str | None) -> dict:
    req = urllib.request.Request(API.format(repo=repo, rev=rev))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _selected(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch.fnmatch(name, g) for g in include):
        return False
    return not any(fnmatch.fnmatch(name, g) for g in exclude)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("revision")
    ap.add_argument("directory")
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-hash", action="store_true", help="size check only")
    ap.add_argument("--token-file")
    ap.add_argument("--file", action="append", default=[], metavar="REPO_PATH=LOCAL_PATH")
    ap.add_argument("--manifest", help="manifest output path (required with --file)")
    args = ap.parse_args(argv)

    mapping: dict[str, Path] = {}
    for item in args.file:
        repo_path, sep, local = item.partition("=")
        if not sep or not repo_path or not local:
            print(f"ERROR: bad --file {item!r}", file=sys.stderr)
            return 2
        mapping[repo_path] = Path(local)
    if mapping and not args.manifest:
        print("ERROR: --file requires --manifest", file=sys.stderr)
        return 2

    root = Path(args.directory)
    if not mapping and not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        listing = fetch_listing(args.repo, args.revision, _token(args.token_file))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot list {args.repo}@{args.revision}: {exc}", file=sys.stderr)
        return 2
    sha = listing.get("sha")
    if not sha or not sha.startswith(args.revision[:7]):
        print(f"ERROR: API returned revision {sha!r}, expected {args.revision}", file=sys.stderr)
        return 2

    if mapping:
        wanted = [s for s in listing.get("siblings", []) if s["rfilename"] in mapping]
        missing = set(mapping) - {s["rfilename"] for s in wanted}
    else:
        wanted = [s for s in listing.get("siblings", []) if _selected(s["rfilename"], args.include, args.exclude)]
        missing = set()
    problems: list[str] = [f"NOT IN REVISION {m}" for m in sorted(missing)]
    to_hash: list[tuple[Path, str, str]] = []
    total = 0
    for s in wanted:
        name = s["rfilename"]
        local = mapping[name] if mapping else root / name
        size = s.get("size")
        if not local.is_file():
            problems.append(f"MISSING {name}")
            continue
        actual = local.stat().st_size
        if size is not None and actual != size:
            problems.append(f"SIZE {name}: local {actual} != published {size}")
            continue
        total += actual
        lfs = s.get("lfs") or {}
        if lfs.get("sha256") and not args.no_hash:
            to_hash.append((local, name, lfs["sha256"]))

    started = time.monotonic()
    verified_hashes: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(sha256_file, p): (n, exp) for p, n, exp in to_hash}
        for fut in cf.as_completed(futures):
            name, expected = futures[fut]
            got = fut.result()
            if got != expected:
                problems.append(f"SHA256 {name}: {got} != {expected}")
            else:
                verified_hashes[name] = got

    if problems:
        for p in problems:
            print(p)
        print(f"FAILED: {len(problems)} problem(s) in {root}")
        return 1

    manifest = {
        "repository": args.repo,
        "revision": sha,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash_checked": not args.no_hash,
        "gated": listing.get("gated", False),
        "files": [
            {"path": s["rfilename"], "size": s.get("size"), "sha256": verified_hashes.get(s["rfilename"]),
             **({"local_path": str(mapping[s["rfilename"]])} if mapping else {})}
            for s in wanted
        ],
        "total_bytes": total,
        "include": args.include,
        "exclude": args.exclude,
    }
    out = Path(args.manifest) if args.manifest else root / ".gx-manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"VERIFIED {args.repo}@{sha}: {len(wanted)} files, {total / 2**30:.2f} GiB, "
        f"{len(verified_hashes)} sha256-checked in {time.monotonic() - started:.0f}s -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
