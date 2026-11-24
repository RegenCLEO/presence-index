"""The panel file: what is asked, of which engines, about which brands.

A panel is frozen before the run. Its SHA-256 covers every field except ``sha256``
itself, and every row a stage writes carries that hash.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

import reference
from scan.engines import ADAPTERS
from scan.records import Market, host_of, owned_by_any

SELECTION_MODES = ("list_plus_ai", "ai_top")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class PanelError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    stratum: str


@dataclass(frozen=True)
class Keyword:
    text: str
    volume: float  # monthly searches; 0 when unknown


@dataclass(frozen=True)
class Brand:
    id: str
    name: str
    domain: str
    other_domains: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    ignore: Tuple[str, ...] = ()   # phrases that contain an alias but are not the brand
    parent: Optional[str] = None
    source: str = "list"           # "list", or "ai_named" for a brand added after the scan
    not_domains: Tuple[str, ...] = ()  # other brands' sites under this brand's domains; set by carve()
    local: bool = True             # False for a business with no store or service area: Local Listings does not apply

    @property
    def domains(self) -> Tuple[str, ...]:
        return (self.domain, *self.other_domains)

    @property
    def names(self) -> Tuple[str, ...]:
        return (self.name, *self.aliases)

    def owns(self, url: Optional[str]) -> bool:
        """True for a URL on the brand's domains, unless it is on another brand's site
        under them. oldnavy.gap.com is Old Navy's site and not Gap's."""
        return owned_by_any(url, self.domains) and not owned_by_any(url, self.not_domains)

    @property
    def names_sha(self) -> str:
        """Changes when a name, alias, ignore phrase or domain does. A verdict read under
        other names is stale."""
        parts = [self.names, self.ignore, self.domains, *([self.not_domains] if self.not_domains else [])]
        text = json.dumps(parts, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Panel:
    panel_id: str
    protocol: str
    category: str
    market: Market
    days: Tuple[str, ...]
    runs_per_day: int
    keyword_pulls: Tuple[str, ...]
    selection_mode: str
    ai_named_max: int
    engines: Dict[str, Dict[str, Any]]
    reader: Dict[str, Dict[str, Any]]
    siteread: Dict[str, Any]
    questions: Tuple[Question, ...]
    keywords: Tuple[Keyword, ...]
    brands: Tuple[Brand, ...]
    sha256: str

    def calls(self, day: str) -> List[Tuple[Question, str, int]]:
        """Every (question, engine, run) to ask on ``day``, run one first."""
        if day not in self.days:
            raise PanelError(f"day {day} is not in the panel's days {list(self.days)}")
        return [(q, engine, run)
                for run in range(1, self.runs_per_day + 1)
                for engine in self.engines
                for q in self.questions]


def digest(data: Dict[str, Any]) -> str:
    """SHA-256 of the panel's content, leaving out the ``sha256`` field."""
    body = {k: v for k, v in data.items() if k != "sha256"}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _need(data: Dict[str, Any], key: str, kind: type) -> Any:
    if key not in data:
        raise PanelError(f"panel is missing '{key}'")
    if not isinstance(data[key], kind):
        raise PanelError(f"panel field '{key}' must be a {kind.__name__}")
    return data[key]


def _days(data: Dict[str, Any], key: str) -> Tuple[str, ...]:
    days = tuple(str(d) for d in _need(data, key, list))
    bad = [d for d in days if not _DAY.match(d)]
    if not days or bad or len(set(days)) != len(days):
        raise PanelError(f"panel field '{key}' must list distinct dates as YYYY-MM-DD")
    return days


def _field(row: Any, key: str, what: str) -> Any:
    """One required field of a question, keyword or the market, as a PanelError when absent."""
    if not isinstance(row, dict) or row.get(key) in (None, ""):
        raise PanelError(f"every {what} needs a '{key}': {row!r}")
    return row[key]


def _unique(ids: List[str], what: str) -> None:
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise PanelError(f"duplicate {what}: {dup}")


def _brand(row: Dict[str, Any], source: str = "list") -> Brand:
    for key in ("id", "name", "domain"):
        if not row.get(key):
            raise PanelError(f"brand {row!r} is missing '{key}'")
    if not _ID.match(str(row["id"])):
        raise PanelError(f"brand id {row['id']!r} must be lower-case letters, digits, '-' or '_'")
    domain = host_of(row["domain"])
    if "." not in domain:
        raise PanelError(f"brand {row['id']} has no usable domain: {row['domain']!r}")
    return Brand(id=str(row["id"]), name=str(row["name"]).strip(), domain=domain,
                 other_domains=tuple(host_of(d) for d in row.get("other_domains") or ()),
                 aliases=tuple(str(a).strip() for a in row.get("aliases") or () if str(a).strip()),
                 ignore=tuple(str(a).strip() for a in row.get("ignore") or () if str(a).strip()),
                 parent=row.get("parent"), source=source, local=row.get("local", True) is not False)


def carve(brands: Sequence[Brand]) -> Tuple[Brand, ...]:
    """Each brand with the domains of the other brands that sit under its own, so a URL
    on such a site counts for the brand whose site it is and for no other."""
    return tuple(
        replace(b, not_domains=tuple(sorted(
            d for other in brands if other.id != b.id for d in other.domains
            if any(d.endswith("." + mine) for mine in b.domains))))
        for b in brands)


def parse(data: Dict[str, Any], verify: bool = True) -> Panel:
    if not isinstance(data, dict):
        raise PanelError("panel file must be a mapping")
    market = _need(data, "market", dict)
    selection = _need(data, "selection", dict)
    mode = selection.get("mode")
    if mode not in SELECTION_MODES:
        raise PanelError(f"selection.mode must be one of {SELECTION_MODES}")

    engines = _need(data, "engines", dict)
    unknown = sorted(set(engines) - set(reference.ENGINE_WEIGHTS_V1))
    if not engines or unknown:
        raise PanelError(f"engines must be drawn from {sorted(reference.ENGINE_WEIGHTS_V1)}; unknown: {unknown}")
    # The method weights Perplexity, but the scan has no adapter for it. Refuse it here,
    # not at the first paid call.
    no_adapter = sorted(e for e, cfg in engines.items() if (cfg or {}).get("adapter", e) not in ADAPTERS)
    if no_adapter:
        raise PanelError(f"the scan has no adapter for {no_adapter}; it can ask {sorted(ADAPTERS)}")

    questions = tuple(Question(str(_field(q, "id", "question")), str(_field(q, "text", "question")).strip(),
                               str(q.get("stratum") or "general"))
                      for q in _need(data, "questions", list))
    _unique([q.id for q in questions], "question ids")
    if not questions or any(not _ID.match(q.id) or not q.text for q in questions):
        raise PanelError("every question needs an id (lower-case letters, digits, '-' or '_') and a text")

    keywords = tuple(Keyword(str(_field(k, "text", "keyword")).strip().lower(), float(k.get("volume") or 0))
                     for k in data.get("keywords") or [])
    _unique([k.text for k in keywords], "keywords")

    brands = carve([_brand(b) for b in data.get("brands") or []])
    _unique([b.id for b in brands], "brand ids")
    # Every domain, other_domains too: a shared domain would count its URLs for both brands.
    _unique([d for b in brands for d in b.domains], "brand domains")
    if mode == "list_plus_ai" and not brands:
        raise PanelError("selection.mode list_plus_ai needs a brand list")

    runs = _need(data, "runs_per_day", int)
    if runs < 1:
        raise PanelError("runs_per_day must be at least 1")

    declared = data.get("sha256")
    actual = digest(data)
    if verify and declared != actual:
        raise PanelError(f"panel hash mismatch: file says {declared}, content is {actual}. "
                         "Run 'python -m scan freeze' only before the scan starts.")

    return Panel(
        panel_id=str(_need(data, "panel_id", str)), protocol=str(_need(data, "protocol", str)),
        category=str(_need(data, "category", str)),
        market=Market(str(_field(market, "country", "market")).upper(), str(_field(market, "language", "market")).lower()),
        days=_days(data, "days"), runs_per_day=runs,
        keyword_pulls=_days(data, "keyword_pulls") if keywords else (),
        selection_mode=mode, ai_named_max=int(selection.get("ai_named_max", 5 if mode == "list_plus_ai" else 20)),
        engines={e: dict(cfg or {}) for e, cfg in engines.items()},
        reader={k: dict(v or {}) for k, v in _need(data, "reader", dict).items()},
        siteread=dict(data.get("siteread") or {}),
        questions=questions, keywords=keywords, brands=brands, sha256=actual)


def load(path: Path, verify: bool = True) -> Panel:
    return parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")), verify=verify)


def freeze(path: Path) -> str:
    """Write the content hash into the file as its last line and return it."""
    path = Path(path)
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("sha256:")]
    text = "\n".join(lines).rstrip("\n") + "\n"
    data = yaml.safe_load(text)
    parse(data, verify=False)
    sha = digest(data)
    path.write_text(f"{text}sha256: {sha}\n", encoding="utf-8")
    return sha


def with_additions(panel: Panel, additions_path: Path) -> Panel:
    """The panel plus what a person confirmed after the scan.

    additions.yaml is append-only and sits beside the outputs, so the panel hash never
    changes. It holds ``brands`` (businesses the answers named, each with the person who
    confirmed its domain) and ``aliases`` (a spelling found in an answer, with that
    answer's id).
    """
    additions_path = Path(additions_path)
    if not additions_path.exists():
        return panel
    extra = yaml.safe_load(additions_path.read_text(encoding="utf-8")) or {}
    brands = {b.id: b for b in panel.brands}
    for row in extra.get("aliases") or []:
        if row.get("brand_id") not in brands or not row.get("alias") or not row.get("answer_id"):
            raise PanelError(f"alias addition needs a known brand_id, an alias and an answer_id: {row!r}")
        b = brands[row["brand_id"]]
        if row["alias"] not in b.names:
            brands[b.id] = replace(b, aliases=(*b.aliases, str(row["alias"]).strip()))
    added = []
    for row in extra.get("brands") or []:
        if not row.get("confirmed_by"):
            raise PanelError(f"added brand {row.get('id')!r} needs 'confirmed_by'")
        b = _brand(row, source="ai_named")
        if b.id in brands or set(b.domains) & {d for x in brands.values() for d in x.domains}:
            raise PanelError(f"added brand {b.id} duplicates a listed brand")
        if b.id in {x.id for x in added} or set(b.domains) & {d for x in added for d in x.domains}:
            raise PanelError(f"added brand {b.id} duplicates another added brand")
        added.append(b)
    if len(added) > panel.ai_named_max:
        raise PanelError(f"{len(added)} brands added, the panel allows {panel.ai_named_max}")
    return replace(panel, brands=carve([*brands.values(), *added]))
