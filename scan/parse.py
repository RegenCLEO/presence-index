"""Read every valid answer for every brand and write one row per answer and brand to mentions.jsonl.

Stage one proposes brands by name, alias and domain, with no model. Stage two has the
reader read each answer twice for its proposed brands, and a third reader settles
the brands where the two values differ. Without --go the stage counts the reads it
would make and stops. With --go a rerun reads only the (answer, brand) pairs that
have no row yet.
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import reference
from scan import panel as panel_file, reader, store
from scan.panel import Brand, Panel, PanelError
from scan.records import MentionRow, mention_id, utc_now

ANSWERS, MENTIONS, READS = "answers.jsonl", "mentions.jsonl", "reads.jsonl"
READ_ERRORS, CANDIDATES, ADDITIONS = "read_errors.jsonl", "candidates.json", "additions.yaml"
READERS = {1: "first", 2: "second", 3: "tiebreak"}


def add_arguments(p) -> None:
    p.add_argument("--go", action="store_true", help="make the reader calls; without it the stage only counts them")
    p.add_argument("--workers", type=int, default=4, help="answers read at once")
    p.add_argument("--attempts", type=int, default=3, help="tries per read before the answer is left for a rerun")
    p.add_argument("--sample-csv", type=Path, help="write a 10%% sample of answers per engine for a person to check")


# ---------- stage one: propose ----------
def _terms(name: str) -> List[str]:
    """The terms a brand name is searched by: the name, and the part before a dot."""
    n = (name or "").strip().lower()
    terms = [n] if len(n) >= 2 else []
    if "." in n:
        core = n.split(".")[0].strip()
        if len(core) >= 3:
            terms.append(core)
    return terms


def _bounded(term: str) -> str:
    return rf"(?<!\w){re.escape(term)}(?!\w)"


def _plain(text: str) -> str:
    """One spelling for what looks the same: curly apostrophes, non-breaking spaces and
    full-width letters would otherwise hide "Macy\u2019s" from the alias "Macy's"."""
    text = unicodedata.normalize("NFKC", text or "")
    return text.translate({0x2019: "'", 0x2018: "'", 0x02BC: "'", 0x2010: "-", 0x2011: "-"}).lower()


def _clean(text: str) -> str:
    """The answer text without Markdown link targets, in one plain spelling."""
    text = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", text or "")
    return _plain(re.sub(r'https?://[^\s<>"{}|\\^`\[\]]+', "", text))


def proposes(brand: Brand, text: str, cited_urls: Sequence[str]) -> bool:
    """True when the answer may name the brand. The reader decides whether it does."""
    if any(brand.owns(u) for u in cited_urls):
        return True
    if any(re.search(_bounded(d), (text or "").lower()) for d in brand.domains):
        return True
    clean = _clean(text)
    for phrase in brand.ignore:
        clean = re.sub(_bounded(_plain(phrase)), "\n", clean)
    names = [_plain(name) for name in brand.names]
    if any(re.search(_bounded(t), clean) for name in names for t in _terms(name)):
        return True
    flat = clean.replace(" ", "")
    return any(len(name) >= 5 and name.replace(" ", "") in flat for name in names)


def own_domain_cited(brand: Brand, cited_urls: Sequence[str]) -> bool:
    return any(brand.owns(u) for u in cited_urls)


# ---------- readers ----------
def reader_specs(panel: Panel) -> Dict[int, Dict[str, Any]]:
    """The three readers by read number. Refuses a tiebreak that is one of the two it settles."""
    specs = {}
    for n, key in READERS.items():
        spec = panel.reader.get(key)
        if not spec or not spec.get("provider"):
            raise PanelError(f"panel reader.{key} needs a provider")
        if spec["provider"] != "fake" and not spec.get("model"):
            raise PanelError(f"panel reader.{key} needs a model")
        specs[n] = spec
    tie = (specs[3]["provider"], specs[3].get("model"))
    if tie[0] != "fake" and tie in {(specs[n]["provider"], specs[n].get("model")) for n in (1, 2)}:
        raise PanelError(f"panel reader.tiebreak {tie} is also the first or second reader; "
                         "the reader that settles a disagreement must be a third model")
    return specs


Have = Dict[str, Tuple[str, reader.Verdict]]  # brand id -> (model, verdict)


def _stored(reads_rows: List[Dict[str, Any]], brands: Dict[str, Brand]) -> Dict[Tuple[str, int], Have]:
    """Verdicts already paid for, by (answer, read number). A crash after a read and
    before its mention rows then costs nothing on the rerun. A verdict is reused only if
    it was read with today's prompt and under the brand's current names."""
    out: Dict[Tuple[str, int], Have] = {}
    for r in reads_rows:
        if not r.get("reply") or r.get("reader_version") != reader.VERSION:
            continue
        reply = reader.Reply.from_dict(r["reply"])
        read_under = r.get("names_sha") or {}
        slot = out.setdefault((r["answer_id"], r["read"]), {})
        slot.update({bid: (r["model"], v) for bid, v in reply.verdicts.items()
                     if bid in brands and read_under.get(bid) == brands[bid].names_sha})
    return out


@dataclass
class Outcome:
    answer: Dict[str, Any]
    attempts: List[Tuple[int, reader.Attempt]] = field(default_factory=list)
    settled: Dict[str, Tuple[str, List[Tuple[str, reader.Verdict]]]] = field(default_factory=dict)
    error: Optional[str] = None


def _read_needed(answer_id: str, proposed: List[Brand], stored, n: int) -> bool:
    """Reads one and two happen for every valid answer, even one that names no listed brand:
    the reply's other businesses are how unlisted brands are found. A later pass reads only
    the brands that have no verdict yet."""
    if (answer_id, n) not in stored:
        return True
    return any(b.id not in stored[(answer_id, n)] for b in proposed)


def _settle(answer: Dict[str, Any], proposed: List[Brand], specs, stored, complete, attempts: int) -> Outcome:
    out = Outcome(answer)
    aid = answer["id"]

    def run(n: int, brands: List[Brand]) -> Optional[Have]:
        have = dict(stored.get((aid, n), {}))
        need = [b for b in brands if b.id not in have]
        if n == 3 and not need:
            return have
        if n == 3 or _read_needed(aid, brands, stored, n):
            tries = reader.read(complete, specs[n], need, answer["text"], aid, n, attempts)
            out.attempts.extend((n, t) for t in tries)
            last = tries[-1]
            if last.reply is None:
                out.error = f"read {n} failed after {len(tries)} attempts: {last.error}"
                return None
            have.update({bid: (last.result.model, v) for bid, v in last.reply.verdicts.items()})
        return have

    first = run(1, proposed)
    second = run(2, proposed) if first is not None else None
    if second is None:
        return out
    split = [b for b in proposed if first[b.id][1].value != second[b.id][1].value]
    third = run(3, split)
    if third is None:
        return out
    for b in proposed:
        reads = [first[b.id], second[b.id]]
        if b in split:
            reads.append(third[b.id])
            out.settled[b.id] = ("tiebreak", reads)
        else:
            out.settled[b.id] = ("agree", reads)
    return out


# ---------- rows ----------
def _row(panel: Panel, answer: Dict[str, Any], brand: Brand, settled) -> MentionRow:
    cited = own_domain_cited(brand, answer["cited_urls"])
    base = {"id": mention_id(answer["id"], brand.id), "panel_id": panel.panel_id,
            "panel_sha256": panel.sha256, "answer_id": answer["id"], "brand_id": brand.id,
            "own_domain_cited": cited, "reader_version": reader.VERSION, "names_sha": brand.names_sha}
    if settled is None:
        return {**base, "value": 0.0, "classification": "NOT_MENTIONED", "polarity": "neutral",
                "position": None, "proposed": False, "reads": [], "settled_by": "not_proposed"}
    how, reads = settled
    final = reads[-1][1] if how == "tiebreak" else reads[0][1]
    return {**base, "value": final.value, "classification": final.classification,
            "polarity": final.polarity, "position": final.position, "proposed": True,
            "reads": [{"model": m, **v.as_dict()} for m, v in reads], "settled_by": how}


def _read_row(panel: Panel, answer_id: str, n: int, spec, attempt: reader.Attempt,
              brands: Dict[str, Brand]) -> Dict[str, Any]:
    result = attempt.result
    return {"id": f"{answer_id}#read{n}", "panel_id": panel.panel_id, "panel_sha256": panel.sha256,
            "answer_id": answer_id, "read": n, "provider": spec["provider"],
            "reader_version": reader.VERSION, "names_sha": {b: brands[b].names_sha for b in attempt.brand_ids},
            "model": result.model if result else (spec.get("model") or ""),
            "brand_ids": attempt.brand_ids, "reply": attempt.reply.as_dict() if attempt.reply else None,
            "raw": None if attempt.reply or not result else result.data,
            "error": attempt.error, "at": utc_now()}


# ---------- candidates ----------
def name_key(name: str) -> str:
    """Case, punctuation, spaces and a trailing '.com' do not make a different business."""
    n = name.strip().lower().rstrip(".")
    n = re.sub(r"\.com$", "", n)
    return re.sub(r"[\W_]+", "", n)


def candidates(panel: Panel, answers: List[Dict[str, Any]], reads_rows, mention_rows) -> Dict[str, Any]:
    weights = reference.ENGINE_WEIGHTS_V1
    engine = {a["id"]: a["engine"] for a in answers if a["valid"]}
    total = sum(weights[e] for e in engine.values())

    def share(answer_ids) -> float:
        return round(sum(weights[engine[a]] for a in answer_ids) / total, 6) if total else 0.0

    known: Dict[str, Brand] = {}
    for b in panel.brands:
        for name in b.names:
            known.setdefault(name_key(name), b)

    spellings: Dict[str, Counter] = {}
    named: Dict[str, set] = {}
    gaps: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in reads_rows:
        if r["read"] not in (1, 2) or not r.get("reply") or r["answer_id"] not in engine:
            continue
        for raw in r["reply"]["other_businesses"]:
            key = name_key(raw)
            if not key:
                continue
            brand = known.get(key)
            if brand is None:
                spellings.setdefault(key, Counter())[raw] += 1
                named.setdefault(key, set()).add(r["answer_id"])
            elif raw.casefold() not in {n.casefold() for n in brand.names}:
                gap = gaps.setdefault((brand.id, raw.casefold()), {"brand_id": brand.id, "spelling": raw,
                                                                   "answer_ids": set()})
                gap["spelling"] = min(gap["spelling"], raw)
                gap["answer_ids"].add(r["answer_id"])

    def spelling(counts: Counter) -> str:
        return min(counts, key=lambda s: (-counts[s], s))

    unlisted = sorted(({"name": spelling(spellings[k]), "key": k, "share": share(ids),
                        "answers": len(ids), "example_answer_ids": sorted(ids)[:5]}
                       for k, ids in named.items()), key=lambda u: (-u["share"], u["name"]))

    listed = {b.id: share({m["answer_id"] for m in mention_rows
                           if m["brand_id"] == b.id and m["value"] > 0 and m["answer_id"] in engine})
              for b in panel.brands if b.source == "list"}
    ranked = sorted(listed.values(), reverse=True)
    tenth = ranked[9] if len(ranked) >= 10 else None
    room = max(0, panel.ai_named_max - sum(1 for b in panel.brands if b.source == "ai_named"))
    if panel.selection_mode == "ai_top":
        enter = unlisted[:room]
    else:
        enter = [u for u in unlisted if tenth is None or u["share"] > tenth][:room]
    return {
        "selection_mode": panel.selection_mode, "valid_answers": len(engine), "total_weight": round(total, 6),
        "unlisted": unlisted,
        "alias_gaps": sorted(({**g, "answer_ids": sorted(g["answer_ids"])} for g in gaps.values()),
                             key=lambda g: (g["brand_id"], g["spelling"])),
        "listed_shares": listed, "tenth_listed_share": tenth,
        "would_enter": [u["name"] for u in enter],
    }


# ---------- person check ----------
def write_sample(path: Path, answers: List[Dict[str, Any]], mention_rows) -> int:
    """A fixed 10% of valid answers per engine, one line per proposed brand."""
    by_engine: Dict[str, List[Dict[str, Any]]] = {}
    for a in answers:
        if a["valid"]:
            by_engine.setdefault(a["engine"], []).append(a)
    rows_by_answer: Dict[str, List[Dict[str, Any]]] = {}
    for m in mention_rows:
        if m["proposed"]:
            rows_by_answer.setdefault(m["answer_id"], []).append(m)
    lines = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["answer_id", "engine", "question_id", "brand_id", "value", "classification",
                    "settled_by", "answer_text"])
        for eng in sorted(by_engine):
            pool = sorted(by_engine[eng], key=lambda a: hashlib.sha256(a["id"].encode()).hexdigest())
            for a in pool[:math.ceil(len(pool) * 0.1)]:
                for m in sorted(rows_by_answer.get(a["id"], []), key=lambda m: m["brand_id"]):
                    w.writerow([a["id"], eng, a["question_id"], m["brand_id"], m["value"],
                                m["classification"], m["settled_by"], a["text"]])
                    lines += 1
    return lines


# ---------- the stage ----------
def _load(path: Path, panel: Panel) -> List[Dict[str, Any]]:
    store.repair(path)
    rows = store.read_jsonl(path)
    store.require_panel(rows, panel.sha256, path)
    return rows


VENDORS = {"gemini": "google"}  # reader provider -> the vendor name prices.yaml uses


def main(args, complete: Optional[reader.Complete] = None) -> int:
    if not args.go:
        return _main(args, complete)
    with store.exclusive(args.out):
        return _main(args, complete)


def _main(args, complete: Optional[reader.Complete] = None) -> int:
    out: Path = args.out
    panel = panel_file.with_additions(panel_file.load(args.panel), out / ADDITIONS)
    specs = reader_specs(panel)
    answers = store.read_jsonl(out / ANSWERS)
    store.require_panel(answers, panel.sha256, out / ANSWERS)
    mention_rows = _load(out / MENTIONS, panel)
    reads_rows = _load(out / READS, panel)
    store.repair(out / READ_ERRORS)

    # A row written under a brand's older names is stale: an alias added since may match an
    # answer that was never read for the brand, or was read without knowing that name.
    # Those rows are dropped and written again, with a fresh read where the brand is proposed.
    brands = {b.id: b for b in panel.brands}
    stale = {m["id"] for m in mention_rows
             if m["brand_id"] in brands and m.get("names_sha") != brands[m["brand_id"]].names_sha}
    if stale:
        print(f"{len(stale)} rows were written before a name was added for their brand; they will be redone.")
        mention_rows = [m for m in mention_rows if m["id"] not in stale]
        if args.go:
            store.rewrite_jsonl(out / MENTIONS, mention_rows)

    done = {(m["answer_id"], m["brand_id"]) for m in mention_rows}
    stored = _stored(reads_rows, brands)
    work = []
    for a in answers:
        if not a["valid"]:
            continue
        missing = [b for b in panel.brands if (a["id"], b.id) not in done]
        proposed = [b for b in missing if proposes(b, a["text"], a["cited_urls"])]
        if missing or any(_read_needed(a["id"], proposed, stored, n) for n in (1, 2)):
            work.append((a, missing, proposed))
    calls = sum(1 for a, _, proposed in work for n in (1, 2) if _read_needed(a["id"], proposed, stored, n))
    valid = sum(1 for a in answers if a["valid"])
    print(f"panel {panel.panel_id}: {valid} valid answers, {len(panel.brands)} brands, "
          f"{len(work)} answers still to parse, {calls} reader calls to make plus a tiebreak call "
          f"for each answer where the two reads disagree")
    if not args.go:
        print("dry run: nothing was read. Pass --go to make the calls.")
        return 0

    if complete is None and any(s["provider"] != "fake" for s in specs.values()):
        from scan.llm import complete_json as complete

    failed = tiebreaks = written = 0
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futures = {pool.submit(_settle, a, proposed, specs, stored, complete, args.attempts): (a, missing)
                   for a, missing, proposed in work}
        for future in as_completed(futures):
            answer, missing = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:  # a bug reading one answer must not stop the others
                outcome = Outcome(answer, error=f"{type(exc).__name__}: {exc}")
            # Cost rows first, then the raw reads, then the rows derived from them.
            for n, attempt in outcome.attempts:
                spec = specs[n]
                if attempt.result is not None and spec["provider"] != "fake":
                    store.log_cost(out, panel_id=panel.panel_id, day=answer["day"], purpose="read",
                                   vendor=VENDORS.get(spec["provider"], spec["provider"]),
                                   ref=f"{answer['id']}#read{n}", model=attempt.result.model,
                                   usage=attempt.result.usage, cost_usd=attempt.result.cost_usd)
                store.append_jsonl(out / READS, _read_row(panel, answer["id"], n, spec, attempt, brands))
            if outcome.error:
                failed += 1
                store.append_jsonl(out / READ_ERRORS, {"id": answer["id"], "at": utc_now(), "error": outcome.error})
                print(f"failed {answer['id']}: {outcome.error}", file=sys.stderr)
                continue
            for b in missing:
                settled = outcome.settled.get(b.id)
                tiebreaks += bool(settled and settled[0] == "tiebreak")
                store.append_jsonl(out / MENTIONS, _row(panel, answer, b, settled))
                written += 1
    finally:
        # On Ctrl-C, a full disk or any other exit, reads not yet started are dropped:
        # nothing would store them.
        pool.shutdown(wait=False, cancel_futures=True)

    mention_rows = store.read_jsonl(out / MENTIONS)
    cands = candidates(panel, answers, store.read_jsonl(out / READS), mention_rows)
    cands["inputs"] = store.input_hashes(out, [ANSWERS, READS, MENTIONS])
    store.write_json(out / CANDIDATES, cands)
    print(f"wrote {written} mention rows, {tiebreaks} settled by the tiebreak, {failed} answers failed to read.")
    print(f"candidates.json: {len(cands['unlisted'])} unlisted businesses, {len(cands['alias_gaps'])} alias gaps, "
          f"would enter: {cands['would_enter'] or 'none'}.")
    if cands["would_enter"] or cands["alias_gaps"]:
        print("A person confirms each one before it counts. In additions.yaml, add a business under 'brands' "
              "with id, name, domain and confirmed_by, and a spelling under 'aliases' with brand_id, alias and "
              "answer_id. Then rerun parse and the stages after it.")
    if args.sample_csv:
        lines = write_sample(args.sample_csv, answers, mention_rows)
        print(f"person-check sample: {lines} lines in {args.sample_csv}")
    if failed:
        print("rerun the same command to retry the answers that failed to read.", file=sys.stderr)
    return 1 if failed else 0
