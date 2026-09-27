"""The safe file manager: browsing, transfer and TRASH inside allowed roots.

Allowed roots are STRICTLY the list in ARCHITECTURE-V41.md section 7
(see config.FILE_ALLOWED_ROOTS):

    /home/legenex/Documents/Projects
    /home/legenex/Documents/Backups
    /home/legenex/Documents/Archive
    /srv/models
    /srv/cache
    /srv/logs

Security rules, all enforced here (never in the browser):

* Every target — parameter of every operation — is resolved with
  ``os.path.realpath()`` (which resolves ``..`` segments AND symlinks) and
  must land inside an allowed root's own realpath. Traversal attempts
  (``../../``, ``/etc/passwd``) and symlink escapes outside the roots are
  refused with 403.
* The trash itself (/srv/cache/trash) is not a browsable target: it is only
  reachable through trash list / restore / purge.
* Mutations (upload, rename, move, mkdir, delete) additionally refuse the
  protected paths (Documents/Backups/GX, the gx-backup repository) and every
  active model path named in the registry (model + engram dirs) — gx-max
  must never be able to delete its own weights from a web page.
* DELETE never deletes: it MOVES the item to /srv/cache/trash/<ts>-<name>
  and appends a manifest entry (original path, ts, size, manifest sha —
  sha of the manifest record, never of the file contents).
* PURGE is a separate, audit-logged action with a typed confirmation token;
  it is the only operation that removes trash permanently.
* Uploads are capped at 512 MiB; previews read at most 64 KiB and refuse
  binary files.

There is no shell involvement: the browser sends paths, this module turns
them into ``Path`` objects after validation. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import UIConfig
from .models import read_registry

MAX_UPLOAD = 512 * 1024 * 1024          # section 7: size cap 512MB
PREVIEW_BYTES = 64 * 1024               # first 64 KiB, text only
SEARCH_LIMIT = 500
NAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")
PURGE_TOKEN = "PURGE TRASH"


class FileManagerError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "file_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileManager:
    def __init__(self, cfg: UIConfig, audit: Callable[..., None] | None = None) -> None:
        self.cfg = cfg
        self.audit = audit or (lambda **kw: None)
        self.roots = tuple(Path(r) for r in cfg.file_roots)
        self.protected = tuple(Path(r) for r in cfg.file_protected)
        self.trash = Path(cfg.trash_root)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ policy
    def _active_model_paths(self) -> tuple[Path, ...]:
        """Model + engram dirs from the registry: never mutable, never deletable."""
        reg = read_registry(self.cfg.registry_path)
        out: list[Path] = []
        for spec in (reg.get("models") or {}).values():
            if isinstance(spec, dict):
                for key in ("path", "engram_dir"):
                    p = spec.get(key)
                    if isinstance(p, str) and p:
                        out.append(Path(p))
        return tuple(out)

    def _root_realpaths(self) -> tuple[tuple[Path, Path], ...]:
        out = []
        for r in self.roots:
            out.append((r, Path(os.path.realpath(r))))
        return tuple(out)

    def resolve(self, target: Any, *, for_write: bool = False) -> Path:
        """Validate a caller-supplied path and return its realpath.

        Raises FileManagerError(403) for anything outside the allowed roots,
        inside the trash, or (for writes) inside protected/model paths.
        """
        if not isinstance(target, str) or not target or "\x00" in target:
            raise FileManagerError("path must be a non-empty string", 400, "invalid_path")
        p = Path(target)
        if not p.is_absolute():
            raise FileManagerError("path must be absolute", 400, "invalid_path")
        real = Path(os.path.realpath(p))  # resolves '..' and symlinks
        trash_real = Path(os.path.realpath(self.trash))
        roots = self._root_realpaths()
        inside = next(((raw, r) for raw, r in roots
                       if real == r or r in real.parents), None)
        if inside is None:
            raise FileManagerError(f"outside the allowed roots: {target[:160]}", 403, "outside_roots")
        if real == trash_real or trash_real in real.parents:
            raise FileManagerError("the trash is managed through restore/purge, not browsed", 403,
                                   "trash_locked")
        if for_write:
            for prot in self.protected:
                prot_real = Path(os.path.realpath(prot))
                if prot_real == real or prot_real in real.parents:
                    raise FileManagerError(f"protected path (read-only): {prot}", 403, "protected")
            for model in self._active_model_paths():
                if model.exists():
                    m_real = Path(os.path.realpath(model))
                    if m_real == real or m_real in real.parents:
                        raise FileManagerError(f"active model path (read-only): {model}", 403,
                                                "model_path")
        return real

    def _entry(self, p: Path) -> dict:
        st = p.stat()
        return {"name": p.name, "path": str(p), "type": "dir" if p.is_dir() else "file",
                "size": st.st_size if p.is_file() else None,
                "mtime": st.st_mtime, "mode": oct(st.st_mode & 0o777)}

    # ----------------------------------------------------------- reading
    def browse(self, path: str) -> dict:
        real = self.resolve(path)
        if not real.is_dir():
            raise FileManagerError("not a directory", 400, "not_dir")
        entries = []
        try:
            for child in sorted(real.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
                if child.name.startswith("."):
                    continue
                entries.append(self._entry(child))
        except OSError as exc:
            raise FileManagerError(f"cannot list: {exc.strerror}", 400) from exc
        roots = [{"path": str(raw), "realpath": str(r)} for raw, r in self._root_realpaths()]
        return {"path": str(real), "entries": entries[:2000], "roots": roots,
               "generated_at": time.time()}

    def search(self, root: str, q: str, limit: int = SEARCH_LIMIT) -> dict:
        real = self.resolve(root)
        if not real.is_dir():
            raise FileManagerError("search root must be a directory", 400, "not_dir")
        q = (q or "").lower()[:200]
        if not q:
            raise FileManagerError("query must not be empty", 400, "invalid_query")
        limit = max(1, min(int(limit or SEARCH_LIMIT), SEARCH_LIMIT))
        hits: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(real):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if q in name.lower():
                    hits.append(self._entry(Path(dirpath) / name))
                    if len(hits) >= limit:
                        return {"root": str(real), "query": q, "results": hits}
        return {"root": str(real), "query": q, "results": hits}

    def dirsize(self, path: str) -> dict:
        real = self.resolve(path)
        if not real.exists():
            raise FileManagerError("no such path", 404, "not_found")
        if real.is_file():
            st = real.stat()
            return {"path": str(real), "bytes": st.st_size, "files": 1, "dirs": 0}
        total, files, dirs = 0, 0, 0
        for dirpath, dirnames, filenames in os.walk(real):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            dirs += len(dirnames)
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                    files += 1
                except OSError:
                    pass
        return {"path": str(real), "bytes": total, "files": files, "dirs": dirs}

    def preview(self, path: str) -> dict:
        real = self.resolve(path)
        if not real.is_file():
            raise FileManagerError("not a file", 400, "not_file")
        size = real.stat().st_size
        with real.open("rb") as fh:
            head = fh.read(PREVIEW_BYTES)
        if b"\x00" in head[:8192]:
            raise FileManagerError("binary file: preview is text-only", 415, "binary")
        return {"path": str(real), "size": size, "truncated": size > len(head),
                "text": head.decode("utf-8", "replace")}

    def download(self, path: str) -> Path:
        real = self.resolve(path)
        if not real.is_file():
            raise FileManagerError("not a file", 404, "not_found")
        return real

    # ---------------------------------------------------------- mutation
    def _safe_name(self, name: Any) -> str:
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.startswith("."):
            raise FileManagerError("invalid name", 400, "invalid_name")
        return name

    def upload(self, dir_path: str, filename: str, writer: Callable[[Path], int]) -> dict:
        """`writer(tmp_path)` streams the body and returns the byte count."""
        real = self.resolve(dir_path, for_write=True)
        if not real.is_dir():
            raise FileManagerError("upload target must be a directory", 400, "not_dir")
        # The WHOLE supplied name is validated (no '/', '\', control chars, no
        # leading dot) — never a basename of it, so "a/b" and "../evil" refuse.
        name = self._safe_name(filename if isinstance(filename, str) else "")
        target = real / name
        # A name that would escape after resolution is impossible (we write to
        # a validated dir + basename), but re-check anyway, belt and braces.
        self.resolve(str(target), for_write=True)
        tmp = real / (".gx-upload-" + os.urandom(6).hex())
        try:
            size = writer(tmp)
            if size > MAX_UPLOAD:
                raise FileManagerError(f"upload exceeds {MAX_UPLOAD // 2**20} MiB", 413, "too_large")
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self.audit(action="files.upload", path=str(target), bytes=size)
        return self._entry(target)

    def rename(self, path: str, new_name: str) -> dict:
        real = self.resolve(path, for_write=True)
        if not real.exists():
            raise FileManagerError("no such path", 404, "not_found")
        name = self._safe_name(new_name)
        target = real.parent / name
        if target.exists():
            raise FileManagerError("a file with that name already exists", 409, "exists")
        os.rename(real, target)
        self.audit(action="files.rename", path=str(real), to=str(target))
        return self._entry(target)

    def move(self, path: str, dest_dir: str) -> dict:
        real = self.resolve(path, for_write=True)
        dest = self.resolve(dest_dir, for_write=True)
        if not dest.is_dir():
            raise FileManagerError("destination must be a directory", 400, "not_dir")
        if dest == real or dest in real.parents:
            raise FileManagerError("cannot move a directory into itself", 400, "invalid_move")
        target = dest / real.name
        if target.exists():
            raise FileManagerError("an item with that name already exists at the destination", 409,
                                   "exists")
        shutil.move(str(real), str(target))
        self.audit(action="files.move", path=str(real), to=str(target))
        return self._entry(target)

    def mkdir(self, dir_path: str) -> dict:
        real = self.resolve(dir_path, for_write=True)
        if real.exists():
            raise FileManagerError("already exists", 409, "exists")
        real.mkdir(parents=False)
        self.audit(action="files.mkdir", path=str(real))
        return self._entry(real)

    # ------------------------------------------------------------- trash
    @property
    def manifest_path(self) -> Path:
        return self.trash / "manifest.jsonl"

    def _manifest(self) -> list[dict]:
        out = []
        try:
            for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def delete(self, path: str, *, user: str = "") -> dict:
        """DELETE = move to /srv/cache/trash/<ts>-<name> + manifest entry."""
        real = self.resolve(path, for_write=True)
        if not real.exists():
            raise FileManagerError("no such path", 404, "not_found")
        size = real.stat().st_size if real.is_file() else self.dirsize(path)["bytes"]
        ts = time.strftime("%Y%m%d-%H%M%S")
        entry_id = os.urandom(8).hex()
        target = self.trash / f"{ts}-{entry_id[:8]}-{real.name[:120]}"
        self.trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(real), str(target))
        record = {"id": entry_id, "original": str(real), "trashed_name": target.name,
                  "ts": time.time(), "size": size, "type": "dir" if real.is_dir() else "file",
                  "user": user[:64]}
        record["manifest_sha"] = _sha(json.dumps({k: v for k, v in record.items()},
                                                 sort_keys=True).encode())
        with self._lock:
            with self.manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        self.audit(action="files.delete", path=str(real), trash_id=entry_id, bytes=size, user=user)
        return {"id": entry_id, "original": str(real), "trashed_name": target.name,
                "size": size, "ts": record["ts"]}

    def trash_list(self) -> dict:
        entries = []
        for rec in self._manifest():
            item = dict(rec)
            item["still_present"] = (self.trash / rec.get("trashed_name", "")).exists()
            entries.append(item)
        return {"trash_root": str(self.trash), "entries": sorted(entries, key=lambda e: -e.get("ts", 0)),
                "purge_token_hint": f"type '{PURGE_TOKEN}' to purge permanently"}

    def restore(self, entry_id: str, *, user: str = "") -> dict:
        if not isinstance(entry_id, str) or not re.fullmatch(r"[0-9a-f]{16}", entry_id):
            raise FileManagerError("invalid trash id", 400, "invalid_id")
        rec = next((e for e in self._manifest() if e.get("id") == entry_id), None)
        if rec is None:
            raise FileManagerError("no such trash entry", 404, "not_found")
        src = self.trash / rec["trashed_name"]
        if not src.exists():
            raise FileManagerError("the trashed item is gone (already purged?)", 404, "not_found")
        original = Path(rec["original"])
        if original.exists():
            raise FileManagerError("the original path exists again; move the item manually", 409,
                                   "exists")
        if not original.parent.is_dir():
            raise FileManagerError("the original parent directory no longer exists", 409, "gone")
        shutil.move(str(src), str(original))
        self._rewrite_manifest([e for e in self._manifest() if e.get("id") != entry_id])
        self.audit(action="files.trash_restore", id=entry_id, path=str(original), user=user)
        return {"id": entry_id, "original": str(original), "restored": True}

    def purge(self, *, user: str = "", confirm: Any = None) -> dict:
        """The ONLY permanent deletion. Separate action, typed token, audited."""
        if confirm is not None and confirm != PURGE_TOKEN:
            raise FileManagerError(f"type '{PURGE_TOKEN}' to purge the trash", 400, "confirm")
        freed, count = 0, 0
        for rec in self._manifest():
            src = self.trash / rec.get("trashed_name", "")
            if src.exists():
                if src.is_dir():
                    shutil.rmtree(src)
                else:
                    freed += src.stat().st_size
                    src.unlink()
                count += 1
        self._rewrite_manifest([])
        self.audit(action="files.trash_purge", count=count, bytes=freed, user=user)
        return {"count": count, "bytes": freed, "purged": True}

    def _rewrite_manifest(self, records: list[dict]) -> None:
        with self._lock:
            self.trash.mkdir(parents=True, exist_ok=True)
            tmp = self.manifest_path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
                           encoding="utf-8")
            tmp.replace(self.manifest_path)
