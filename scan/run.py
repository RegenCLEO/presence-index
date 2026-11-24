"""Ask every panel question on every engine and append the raw answers to answers.jsonl.

Without --go the stage lists the calls it would make and stops. With --go it skips
every answer already in answers.jsonl, so a rerun finishes what an earlier run left.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scan import engines, panel as panel_file, store
from scan.records import AnswerRow, EngineError, EngineResult, answer_id, utc_now

ANSWERS, ERRORS = "answers.jsonl", "errors.jsonl"


def add_arguments(p) -> None:
    p.add_argument("--day", help="scan day, one of the panel's days (default: the panel's only day)")
    p.add_argument("--go", action="store_true", help="make the calls; without it the stage only lists them")
    p.add_argument("--attempts", type=int, default=3, help="tries per call before it is left for a rerun")
    p.add_argument("--limit", type=int, help="ask only the first N questions (rehearsal)")
    p.add_argument("--any-day", action="store_true",
                   help="ask even when today is not the scan day (rehearsal, in its own output directory)")


class Failed(Exception):
    """Every attempt failed. ``billed`` holds what the vendor charged for them."""

    def __init__(self, message: str, billed: List[float]):
        super().__init__(message)
        self.billed = billed


def _ask(engine: str, question: str, market, config: Dict[str, Any],
         attempts: int) -> Tuple[EngineResult, List[float]]:
    """(result, what failed attempts cost). Raises Failed when no attempt succeeds."""
    last: Optional[Exception] = None
    billed: List[float] = []
    for attempt in range(1, attempts + 1):
        try:
            return engines.ask(engine, question, market, config), billed
        except EngineError as exc:
            last = exc
            if exc.cost_usd is not None:
                billed.append(exc.cost_usd)
            if attempt < attempts:
                time.sleep(2 * attempt)
        except Exception as exc:  # an adapter bug must not stop the other calls
            last = exc
            break
    raise Failed(f"{type(last).__name__}: {last}", billed)


def check_day(day: str, any_day: bool) -> None:
    """A scan day's answers are asked on that day. One day either side allows for time zones."""
    today = datetime.now(timezone.utc).date()
    if not any_day and abs((date.fromisoformat(day) - today).days) > 1:
        raise panel_file.PanelError(f"scan day {day} is not today ({today} UTC). Answers stored under a day must be "
                                    "asked on it. For a rehearsal pass --any-day and use its own output directory.")


def main(args) -> int:
    if not args.go:
        return _main(args)
    with store.exclusive(args.out):
        return _main(args)


def _main(args) -> int:
    panel = panel_file.load(args.panel)
    out: Path = args.out
    day = args.day or (panel.days[0] if len(panel.days) == 1 else None)
    if day is None:
        raise panel_file.PanelError(f"the panel has several days, pass --day (one of {list(panel.days)})")
    calls = panel.calls(day)
    if args.limit:
        keep = {q.id for q in panel.questions[:args.limit]}
        calls = [c for c in calls if c[0].id in keep]

    answers = out / ANSWERS
    store.repair(answers)
    existing = store.read_jsonl(answers)
    store.require_panel(existing, panel.sha256, answers)
    done = {r["id"] for r in existing}
    if any(r.get("off_day") for r in existing) and not getattr(args, "any_day", False):
        raise store.StoreError(f"{answers} holds rehearsal answers asked on another day. A scan needs its own "
                               "output directory.")
    todo = [(q, e, r) for q, e, r in calls if answer_id(day, e, q.id, r) not in done]

    per_engine = {e: sum(1 for _, x, _ in todo if x == e) for e in panel.engines}
    print(f"panel {panel.panel_id} day {day}: {len(calls)} calls, {len(calls) - len(todo)} already stored, "
          f"{len(todo)} to make {per_engine}")
    if not args.go:
        print("dry run: nothing was asked. Pass --go to make the calls.")
        return 0
    check_day(day, getattr(args, "any_day", False))
    for engine, config in panel.engines.items():
        engines.check(engine, config)

    off_day = abs((date.fromisoformat(day) - datetime.now(timezone.utc).date()).days) > 1

    def billed_failures(row_id: str, engine: str, costs: List[float]) -> None:
        for cost in costs:
            store.log_cost(out, panel_id=panel.panel_id, day=day, purpose="answer",
                           vendor=engines.vendor(engine, panel.engines[engine]), ref=f"{row_id}#failed",
                           cost_usd=cost)

    # One pool per engine, sized to that engine's limit: a slow engine never holds up the others.
    pools = {e: ThreadPoolExecutor(max_workers=max(1, int(cfg.get("concurrency", 2))))
             for e, cfg in panel.engines.items()}
    failed = 0
    try:
        futures = {pools[e].submit(_ask, e, q.text, panel.market, panel.engines[e], args.attempts): (q, e, r)
                   for q, e, r in todo}
        for future in as_completed(futures):
            q, engine, run = futures[future]
            row_id = answer_id(day, engine, q.id, run)
            try:
                result, wasted = future.result()
            except Failed as exc:
                failed += 1
                billed_failures(row_id, engine, exc.billed)
                store.append_jsonl(out / ERRORS, {"id": row_id, "at": utc_now(), "error": str(exc)})
                print(f"failed {row_id}: {exc}", file=sys.stderr)
                continue
            row: AnswerRow = {
                "id": row_id, "panel_id": panel.panel_id, "panel_sha256": panel.sha256, "day": day,
                "question_id": q.id, "engine": engine, "run": run, "model": result.model,
                "settings": result.settings, "asked_at": utc_now(), "raw": result.raw, "text": result.text,
                "cited_urls": result.cited_urls, "valid": result.valid, "invalid_reason": result.invalid_reason,
            }
            if off_day:
                row["off_day"] = True
            billed_failures(row_id, engine, wasted)
            # The cost row goes first: a crash between the two writes then leaves a paid
            # call on the log and no answer, and the rerun asks again and logs that call too.
            store.log_cost(out, panel_id=panel.panel_id, day=day, purpose="answer",
                           vendor=engines.vendor(engine, panel.engines[engine]), ref=row_id,
                           model=result.model, usage=result.usage, cost_usd=result.cost_usd)
            store.append_jsonl(answers, row)
    finally:
        # On Ctrl-C, a full disk or any other exit, calls not yet started are dropped:
        # nothing would store their answers. Calls already in flight finish and are lost.
        for pool in pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
    stored = len(todo) - failed
    print(f"stored {stored} answers, {failed} failed. Cost so far: {store.cost_summary(out)}")
    if failed:
        print("rerun the same command to retry the failed calls.", file=sys.stderr)
    return 1 if failed else 0
