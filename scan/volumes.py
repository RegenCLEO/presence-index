"""Look up search volumes for keyword candidates and fill a draft panel's keywords.

A step in building a panel, run before the freeze. One DataForSEO call
(keywords_data/google_ads/search_volume/live) covers every candidate. Without --go the
stage says what it would send and stops. The reply is saved in the output directory, so
a rerun with the same candidates makes no call and only ranks them again.

The candidates file has one keyword on a line. A ``[name]`` line starts a group, and a
line that starts with ``#`` is a comment.

Two rules remove a candidate before the highest volumes are kept:
  1. It contains a word given with --exclude-word.
  2. It differs from another candidate only by a plural "s" or by word order. Google
     reports one combined volume for such a pair, so keeping both counts it twice. The
     one with the higher volume stays, and a tie goes to the first in alphabetical order.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from scan import store
from scan.keywords import LOCATION_CODES, Post, dataforseo_post, task_of
from scan.panel import PanelError
from scan.records import EngineError

ENDPOINT = "keywords_data/google_ads/search_volume/live"
LOOKUP, TABLE = "volume-lookup.json", "keyword-volumes.csv"
MAX_KEYWORDS, MAX_CHARS, MAX_WORDS = 1000, 80, 10   # the endpoint's limits on one task
TIMEOUT_S = 120.0
_TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*\s*:")

Row = Tuple[int, str, str, bool]   # volume, keyword, group, whether the reply held it


def add_arguments(p) -> None:
    p.add_argument("--candidates", required=True, type=Path, help="text file of keyword candidates")
    p.add_argument("--keep", type=int, default=100, help="how many keywords the panel takes (default 100)")
    p.add_argument("--exclude-word", action="append", default=[], metavar="WORD",
                   help="leave out every candidate that contains this word; repeat for more words")
    p.add_argument("--go", action="store_true", help="make the call; without it the stage only says what it would send")


# ---------- pure rules ----------
def candidates(path: Path) -> List[Tuple[str, str]]:
    """(keyword, group) for each candidate line, lower-cased."""
    out: List[Tuple[str, str]] = []
    group = ""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1].strip()
            continue
        out.append((" ".join(line.lower().split()), group))
    keywords = [kw for kw, _ in out]
    twice = sorted({kw for kw in keywords if keywords.count(kw) > 1})
    too_long = [kw for kw in keywords if len(kw) > MAX_CHARS or len(kw.split()) > MAX_WORDS]
    if not out:
        raise PanelError(f"{path} holds no keyword")
    if twice:
        raise PanelError(f"candidates listed twice: {twice}")
    if too_long:
        raise PanelError(f"Google Ads takes a keyword of {MAX_CHARS} characters and {MAX_WORDS} words at most: {too_long}")
    if len(out) > MAX_KEYWORDS:
        raise PanelError(f"{len(out)} candidates; one lookup takes {MAX_KEYWORDS} at most")
    return out


def volumes(task: Dict[str, Any]) -> Dict[str, int]:
    """keyword -> monthly searches, for the keywords the reply holds."""
    if task.get("status_code") != 20000:
        raise EngineError(f"DataForSEO task error {task.get('status_code')}: {task.get('status_message')}")
    return {str(row["keyword"]).lower(): int(row.get("search_volume") or 0)
            for row in task.get("result") or [] if isinstance(row, dict) and row.get("keyword")}


def removed(found: Dict[str, int], keywords: List[str], exclude_words: List[str]) -> Dict[str, str]:
    """keyword -> why it cannot enter the panel."""
    out: Dict[str, str] = {}
    for kw in keywords:
        hit = sorted(set(kw.split()) & set(exclude_words))
        if hit:
            out[kw] = f"contains the excluded word '{hit[0]}'"
    same: Dict[Tuple[str, ...], List[str]] = {}
    for kw in keywords:
        if kw not in out:
            same.setdefault(tuple(sorted(word.removesuffix("s") for word in kw.split())), []).append(kw)
    for group in same.values():
        first, *rest = sorted(group, key=lambda kw: (-found.get(kw, 0), kw))
        out.update({kw: f"near duplicate of '{first}'" for kw in rest})
    return out


def with_keywords(text: str, block: List[str]) -> str:
    """The panel text with its ``keywords:`` block replaced by ``block``. Comments and
    blank lines directly above the next top-level key stay with that key."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("keywords:")), None)
    if start is None:
        return "\n".join([*lines, "", *block]) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _TOP_LEVEL_KEY.match(lines[i])), len(lines))
    if end < len(lines):
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
            end -= 1
        block = [*block, ""]
    return "\n".join([*lines[:start], *block, *lines[end:]]) + "\n"


# ---------- the stage ----------
def main(args, post: Optional[Post] = None) -> int:
    text = Path(args.panel).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PanelError("panel file must be a mapping")
    if data.get("sha256"):
        raise PanelError("the panel is frozen, so its keywords cannot change")
    market = data.get("market") or {}
    country, language = str(market.get("country") or "").upper(), str(market.get("language") or "").lower()
    if country not in LOCATION_CODES or not language:
        raise PanelError(f"the panel's market needs a language and a country in {sorted(LOCATION_CODES)}")
    if args.keep < 1:
        raise PanelError("--keep must be at least 1")
    exclude_words = [word.lower() for word in args.exclude_word]

    rows = candidates(args.candidates)
    request = {"keywords": sorted(kw for kw, _ in rows), "location_code": LOCATION_CODES[country],
               "language_code": language}
    out: Path = args.out
    lookup = store.read_json(out / LOOKUP) if (out / LOOKUP).exists() else None
    if lookup and lookup.get("request") == request:
        print(f"using the reply saved on {lookup['fetched_on']} in {out / LOOKUP}; no call made.")
    else:
        print(f"one call to {ENDPOINT}: {len(rows)} keywords, location_code {request['location_code']}, "
              f"language {language}")
        if not args.go:
            print("dry run: nothing was asked. Pass --go to make the call.")
            return 0
        try:
            with store.exclusive(out):
                lookup = fetch(out, str(data.get("panel_id") or ""), request, post or dataforseo_post(TIMEOUT_S))
        except EngineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    found = volumes(task_of(lookup["reply"]))
    out_of_panel = removed(found, [kw for kw, _ in rows], exclude_words)
    by_volume: List[Row] = sorted(((found.get(kw, 0), kw, group, kw in found) for kw, group in rows),
                                  key=lambda row: (-row[0], row[1]))
    ranked = [row for row in by_volume if row[1] not in out_of_panel]
    kept = ranked[:args.keep]
    write_table(out / TABLE, by_volume, {row[1] for row in kept}, out_of_panel)
    if len(kept) < args.keep or kept[-1][0] <= 0:
        with_volume = sum(1 for row in ranked if row[0] > 0)
        raise PanelError(f"{with_volume} candidates have a volume after the two rules and the panel needs "
                         f"{args.keep}. {out / TABLE} is written and the panel is unchanged. Add candidates.")

    new_text = with_keywords(text, keyword_block(kept, lookup["fetched_on"], country, exclude_words))
    wanted = [{"text": kw, "volume": volume} for volume, kw, _, _ in kept]
    if yaml.safe_load(new_text).get("keywords") != wanted:
        raise PanelError(f"could not place the keywords block in {args.panel}; the panel is unchanged")
    Path(args.panel).write_text(new_text, encoding="utf-8")

    total = sum(row[0] for row in kept)
    dropped = ranked[args.keep][0] if len(ranked) > args.keep else "none"
    print(f"{len(out_of_panel)} candidates removed by rule, {len(kept)} of {len(ranked)} kept. "
          f"Lowest kept volume {kept[-1][0]}, highest dropped {dropped}.")
    print(f"'{kept[0][1]}' carries {100 * kept[0][0] / total:.1f}% of the kept volume, which is its "
          f"share of the Organic Search weight.")
    print(f"wrote {out / TABLE} and the keywords block of {args.panel}")
    return 0


def fetch(out: Path, panel_id: str, request: Dict[str, Any], post: Post) -> Dict[str, Any]:
    day = datetime.now(timezone.utc).date().isoformat()
    reply = post(ENDPOINT, [request])
    task = task_of(reply)
    if task.get("cost") is not None:
        # Logged before the volumes are read: a task that bills and then fails is still paid.
        store.log_cost(out, panel_id=panel_id, day=day, purpose="volumes", vendor="dataforseo",
                       ref=f"{day}/volumes", model=ENDPOINT, cost_usd=float(task["cost"]))
    volumes(task)
    lookup = {"request": request, "fetched_on": day, "reply": reply}
    store.write_json(out / LOOKUP, lookup)
    return lookup


def write_table(path: Path, by_volume: List[Row], kept: set, out_of_panel: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        table = csv.writer(handle)
        table.writerow(["keyword", "group", "volume", "returned", "kept", "removed_because"])
        for volume, kw, group, returned in by_volume:
            table.writerow([kw, group, volume, "yes" if returned else "no",
                            "yes" if kw in kept else "no", out_of_panel.get(kw, "")])


def keyword_block(kept: List[Row], fetched_on: str, country: str, exclude_words: List[str]) -> List[str]:
    left_out = "a keyword that differs from a kept one only by a plural or by word order"
    if exclude_words:
        left_out = f"keywords with the word {' or '.join(repr(word) for word in exclude_words)}, and {left_out}"
    return ["keywords:",
            f"  # Written by `python -m scan volumes`. The {len(kept)} candidates with the highest monthly",
            f"  # search volume in {country}, from Google Ads through a data provider, looked up on {fetched_on}.",
            "  # Ties sort by keyword text. Left out before the cut:",
            f"  # {left_out}.",
            *(f"  - {{text: {json.dumps(kw, ensure_ascii=False)}, volume: {volume}}}" for volume, kw, _, _ in kept)]
