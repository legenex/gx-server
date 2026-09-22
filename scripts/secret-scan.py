#!/usr/bin/env python3
"""Fail if the GX Backup git tree looks like it contains plaintext secrets."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"age-secret-key-1[a-z0-9]+"),
    re.compile(r"RESTIC_PASSWORD\s*="),
    re.compile(r"LITELLM_MASTER_KEY\s*=\s*sk-"),
    re.compile(r"password\s*=\s*['\"][^'\"]{8,}", re.I),
]
SKIP = (".git/", "package-manifests/", "docs/CURRENT-SYSTEM-AUDIT.md")


def tracked() -> list[str]:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
    if r.returncode != 0:
        files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
        return files
    return [ln for ln in r.stdout.splitlines() if ln]


def main() -> int:
    hits = []
    for rel in tracked():
        if any(rel.startswith(s) or f"/{s}" in rel for s in SKIP):
            continue
        path = ROOT / rel
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in PATTERNS:
            if pat.search(text):
                hits.append(f"{rel}: {pat.pattern[:40]}")
                break
    if hits:
        print("SECRET SCAN FAIL")
        for h in hits:
            print(" ", h)
        return 1
    print("SECRET SCAN PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
