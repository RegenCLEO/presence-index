"""Files on disk: append-only JSONL rows, whole-file JSON, hashes and the cost log.

One process writes a file at a time. A row is written with one ``write`` call and
synced, so a crash can only leave a cut-off last line, which ``read_jsonl`` drops and
``repair`` removes before the next append.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set

import yaml

from scan.records import CostRow, utc_now

PRICES_PATH = Path(__file__).with_name("prices.yaml")


class StoreError(RuntimeError):
    pass


# ---------- JSONL ----------
def _encode(row: Dict[str, Any]) -> bytes:
    """One JSON line. default=str: a value JSON cannot hold (a datetime inside a vendor
    response) is kept as text. backslashreplace: half a surrogate pair in vendor text is
    kept as its escape. A paid answer is never dropped over its formatting."""
    return (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8", "backslashreplace")


def _lines(path: Path) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        yield from fh


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Every complete row. A cut-off last line is skipped; a bad line elsewhere is an error."""
    path = Path(path)
    if not path.exists():
        return []
    rows, lines = [], list(_lines(path))
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        if not line.strip():
            continue
        try:
            if last and not line.endswith(b"\n"):
                raise ValueError("no newline")
            rows.append(json.loads(line))
        except ValueError as exc:
            if last:
                break
            raise StoreError(f"{path}: line {i + 1} is not valid JSON") from exc
    return rows


def repair(path: Path) -> int:
    """Cut a torn last line off the file. Returns the number of bytes removed."""
    path = Path(path)
    if not path.exists():
        return 0
    data = path.read_bytes()
    keep = len(data)
    if data and not data.endswith(b"\n"):
        keep = data.rfind(b"\n") + 1
    else:
        tail_start = data.rfind(b"\n", 0, len(data) - 1) + 1 if data else 0
        tail = data[tail_start:]
        if tail.strip():
            try:
                json.loads(tail)
            except ValueError:
                keep = tail_start
    if keep != len(data):
        with open(path, "r+b") as fh:
            fh.truncate(keep)
            fh.flush()
            os.fsync(fh.fileno())
    return len(data) - keep


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _encode(row)
    with open(path, "ab") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def rewrite_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    """Replace a derived JSONL file in one step. Never used on answers.jsonl."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        for row in rows:
            fh.write(_encode(row))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextmanager
def exclusive(out_dir: Path):
    """Hold the output directory for one writing stage. Two stages appending to the same
    files at once would duplicate rows. The lock is an OS file lock, so it goes when its
    process does and two starters cannot both take it."""
    import fcntl

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(out_dir / ".lock", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.read(fd, 32).decode(errors="replace").strip() or "unknown"
            raise StoreError(f"another scan stage (process {holder}) is writing to {out_dir}. Wait for it to finish.")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        repair(out_dir / "cost_log.jsonl")
        yield
    finally:
        os.close(fd)


def ids(path: Path) -> Set[str]:
    return {row["id"] for row in read_jsonl(path)}


def require_panel(rows: Iterable[Dict[str, Any]], panel_sha256: str, path: Path) -> None:
    """Refuse rows written under a different panel."""
    other = {r.get("panel_sha256") for r in rows} - {panel_sha256}
    if other:
        raise StoreError(f"{path} holds rows from another panel ({sorted(str(o) for o in other)}). "
                         "Use a new output directory for a new or changed panel.")


# ---------- whole files ----------
def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def file_sha256(path: Path) -> Optional[str]:
    path = Path(path)
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_hashes(out_dir: Path, names: Iterable[str]) -> Dict[str, Optional[str]]:
    """SHA-256 of each input file a stage read, recorded in what it writes."""
    return {name: file_sha256(Path(out_dir) / name) for name in names}


# ---------- cost log ----------
def price(vendor: str, model: str, usage: Dict[str, Any], prices_path: Path = PRICES_PATH) -> Optional[float]:
    """USD for a call billed by usage, from the dated price table. None when the table
    has no entry for the model, or the usage names a unit the entry does not price."""
    table = (yaml.safe_load(Path(prices_path).read_text(encoding="utf-8")) or {}).get("prices") or {}
    entry = (table.get(vendor) or {}).get(model)
    if not entry:
        return None
    per_unit = entry.get("usd_per_unit") or {}
    total = 0.0
    for unit, count in usage.items():
        if not isinstance(count, (int, float)) or isinstance(count, bool) or not count:
            continue
        if unit not in per_unit:
            return None
        total += count * per_unit[unit]
    return round(total, 6)


def log_cost(out_dir: Path, *, panel_id: str, day: str, purpose: str, vendor: str, ref: str,
             model: str = "", usage: Optional[Dict[str, Any]] = None,
             cost_usd: Optional[float] = None) -> CostRow:
    """One row per paid call. A cost the vendor returned is used as is; otherwise the
    usage is priced from prices.yaml, and a call nothing can price is logged unpriced."""
    usage = dict(usage or {})
    if cost_usd is not None:
        basis = "vendor"
    else:
        cost_usd = price(vendor, model, usage)
        basis = "prices" if cost_usd is not None else "unpriced"
    row: CostRow = {"panel_id": panel_id, "day": day, "purpose": purpose, "vendor": vendor, "ref": ref,
                    "model": model, "usage": usage,
                    "cost_usd": cost_usd, "basis": basis, "at": utc_now()}
    append_jsonl(Path(out_dir) / "cost_log.jsonl", row)
    return row


def cost_summary(out_dir: Path) -> Dict[str, Any]:
    rows = read_jsonl(Path(out_dir) / "cost_log.jsonl")
    by_purpose: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        slot = by_purpose.setdefault(r["purpose"], {"calls": 0, "cost_usd": 0.0, "unpriced_calls": 0})
        slot["calls"] += 1
        if r["cost_usd"] is None:
            slot["unpriced_calls"] += 1
        else:
            slot["cost_usd"] = round(slot["cost_usd"] + r["cost_usd"], 6)
    return {"calls": len(rows), "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in rows), 6),
            "unpriced_calls": sum(1 for r in rows if r["cost_usd"] is None), "by_purpose": by_purpose}
