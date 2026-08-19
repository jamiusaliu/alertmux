# Architecture

How alertmux is put together and why each part is shaped the way it is.

> **Keeping this current:** when you add a module or change a boundary, update the
> map below and the relevant section. When you add a *defence* — a guard against a
> specific real-world failure — record what failure it guards, with evidence. A
> guard whose reason is forgotten is a guard someone deletes.

## The shape

```
official feeds ──> adapters ──> NormalisedAlert[] ──> query ──> api
                   swic.py                            collect   /alerts
                   usgs.py                                      /health
                                                                 /sources
```

One direction. No writes to any external system. No state except a 60-second cache.

| Module | Responsibility |
|---|---|
| `schema.py` | `NormalisedAlert`, `Provenance`, `DISCLAIMER` — the contract |
| `adapters/base.py` | `FetchResult`, the `Adapter` protocol |
| `adapters/swic.py` | WMO SWIC — 59 national alerting authorities |
| `adapters/usgs.py` | USGS earthquakes |
| `adapters/__init__.py` | Adapter registry — `default_adapters()` |
| `query.py` | Aggregation, partial-result labelling |
| `dedupe.py` | Cross-source duplicate reporting — never merges or drops |
| `sources.py` | `/sources` report: structural gaps, authorities seen, heuristic hazard coverage |
| `registry.py` | `/authorities` — WMO Register of Alerting Authorities (directory, not alerts), embedded ISO 3166-1 alpha-3→alpha-2 table, country-level coverage join, long-TTL cache |
| `api.py` | FastAPI, TTL cache, HTTP status semantics |
| `mcp_server.py` | MCP presentation (optional `mcp` extra) — four tools over the same cached `collect()` path |
| `notify/config.py` | Notifier config — TOML plus environment overrides, SecretStr-guarded credentials |
| `notify/rules.py` | Pure rule matching over `NormalisedAlert`, no I/O — the unmapped-severity counting and safe-default logic |
| `notify/state.py` | Persistent JSON seen-state, atomic writes, prunable |
| `notify/delivery.py` | SMTP delivery via stdlib `smtplib`, verbatim relay message building |
| `notify/runner.py` | One poll cycle: expiry filter, cross-source dedupe, rule evaluation, rate limit, digest, delivery, state |
| `notify/runlog.py` | Append-only JSONL log of every real run's outcome (sent/suppressed/failed), so a failed run leaves a trace after the process exits |
| `notify/cli.py` | `alertmux-notify` console script |
| `dashboard/collector.py` | The dashboard's own TTL-cached `collect()` wrapper, plus cross-poll source-health memory (last OK, consecutive failures) |
| `dashboard/volume.py` | Append-only JSONL volume-history recorder, one snapshot per fresh fetch |
| `dashboard/app.py` | FastAPI, its own app entirely separate from `api.py` — read-only, GET-only |
| `dashboard/page.py` | The single self-contained HTML page (inline CSS/JS, no build step) |
| `dashboard/cli.py` | `alertmux-dashboard` console script |

An adapter knows its own source's quirks and **nothing** about any other adapter,
the query layer, or the API. That isolation is what makes adding a feed a one-file
change, and it is the property to protect above convenience.

## schema.py — the contract

`Provenance` is mandatory on every alert and has no default, so pydantic rejects any
alert that lacks it. It is not possible to construct hazard data in this system
without recording where it came from.

The central pairing in `NormalisedAlert`:

```python
severity: str | None = None          # our interpretation — "Severe"
source_severity: str | None = None   # what the source said — "3"
```

**Both always travel together.** The named CAP level is an interpretation; the raw
value is the authority's own words. If a mapping is ever wrong, the truth is still
in the response and a consumer can recover. This is why a mapping error here is
correctable rather than silent corruption.

`unavailable_fields` exists because `expires: null` is ambiguous — it could mean the
alert never expires, or that the source did not say. Naming the gap removes the
ambiguity. The README states this list is *exhaustive*; that promise is enforced
mechanically (see below), not by remembering to append.

`unavailable_fields` alone was still ambiguous in a second way: `severity: null`
could mean "the source said nothing" or "the source said something we refused to
translate" — `source_severity` had to be inspected field-by-field to tell them
apart. `unmapped_fields` splits that second case out: a field the source *did*
supply but that this adapter declined to map (an SWIC code outside the D1 tables,
GDACS's `alertlevel`, a tsunami bulletin category, the USGS PAGER level) lands
there instead, never in `unavailable_fields`. A `STRUCTURAL_GAPS` entry always
means "this feed never supplies the concept at all" and always stays in
`unavailable_fields` regardless of any particular record — see DECISIONS.md's
entry on this split for the per-adapter reasoning.

## adapters/base.py — adapters never raise, and neither does one bad record

A source being down is **data**, not an exception:

```python
class FetchResult(BaseModel):
    source_id: str
    ok: bool
    alerts: list[NormalisedAlert] = []
    error: str | None = None
    truncated: bool = False
    matched: int | None = None
    returned: int | None = None
    invalid_count: int = 0
    invalid_samples: list[str] = []
    duplicate_count: int = 0
```

`fetch()` catches broadly and returns `ok=False`. This is what stops one broken feed
taking down the whole response — but that guard is about the *envelope*: a 200 that
isn't valid GeoJSON/RSS at all. A malformed *record* inside an otherwise-good
envelope is a different failure and gets a different treatment (D3, superseded
2026-08-18): **per-feature quarantine**, not a fetch-wide abort.

```python
class RecordQuarantine(list):
    """A list of parsed alerts that also carries the stats for the
    records that failed to parse instead of raising."""

    def __init__(self, alerts=None):
        super().__init__(alerts or [])
        self.invalid_count = 0
        self.invalid_samples: list[str] = []

    def quarantine(self, exc: Exception) -> None:
        self.invalid_count += 1
        if len(self.invalid_samples) < RECORD_SAMPLE_CAP:
            self.invalid_samples.append(f"{type(exc).__name__}: {exc}")
```

Every adapter's `parse()` loop wraps the per-record body in `try`/`except Exception`
and calls `alerts.quarantine(exc)` on failure instead of letting the exception
propagate. `RecordQuarantine` subclasses `list` deliberately: every existing caller
of `parse()` — every other adapter, every test — treats the return value as a plain
`list[NormalisedAlert]` (`len()`, indexing, `== []`, iteration). Subclassing keeps
every one of those call sites working unchanged; a caller that needs the quarantine
stats reads `.invalid_count` / `.invalid_samples` off the same object. `fetch()`
copies both onto `FetchResult`. Samples are capped (`RECORD_SAMPLE_CAP = 5`) so a
source that reshaped its whole schema produces five diagnosable exception strings,
not a fetch-sized wall of identical ones — the count itself stays exact regardless
of the cap.

Envelope checks (`_require_feature_collection`, GDACS's `<rss>`/`<channel>` check,
EONET's events-list check) stay **outside** the per-record loop and still raise:
a broken envelope means the source itself is broken, not that one record was bad.
Collapsing that distinction was explicitly ruled out — see D3 in `DECISIONS.md`.

`truncated` is deliberately separate from `ok`, on the same reasoning `invalid_count`
now follows: the source answered correctly and the alerts present are real, but
there were more (`truncated`) or some were dropped as unparseable (`invalid_count`).
Either way it is a right answer that is incomplete.

**Pagination (issue #3, D4).** `fetch()` loops on `startIndex` rather than
requesting one capped page: it keeps requesting further pages while the last
one came back filled to `maxFeatures`, or `numberMatched` says the cumulative
offset is still short of the total, bounded by `max_pages` (default 20).
Hitting that ceiling — or a page contributing zero records not already
seen — stops the loop with `truncated` left True; a full round of pagination
that reaches a genuinely short final page leaves it False. Records are
deduplicated across pages by id, and a collapsed duplicate is counted in
`duplicate_count` rather than silently dropped or double-counted.

## adapters/swic.py — three defences

Each guards a failure that actually occurred during development. See
`DATA-SOURCES.md` for the evidence and `DECISIONS.md` for the reasoning.

**1. A 200 is not necessarily success.**

```python
def _require_feature_collection(payload: dict) -> None:
    if payload.get("type") != "FeatureCollection" or "features" not in payload:
        raise ValueError(...)
```

GeoServer answers a bad `cql_filter`, an unknown `typeName`, or backend trouble with
an OWS exception report at **HTTP 200** and no `features` key. Without this guard,
`payload.get("features", [])` yields zero alerts and every source reports healthy —
the system would say "no hazards anywhere on Earth" while completely broken.

**2. Identity comes from content, not from the server.**

```python
capurl = props.get("capurl")
if not capurl:
    raise ValueError("...the synthetic GeoServer fid is not a stable identity")
id=f"{self.source_id}:{capurl}"
```

GeoServer's `feature["id"]` embeds a *request* timestamp, so it changes on every
fetch. `capurl` identifies the CAP file itself and is stable. Deduplication — and
therefore any future notifier — rests entirely on this. This `raise` is inside the
per-feature `try` in `parse()`'s loop, so as of D3 it no longer aborts the whole
fetch: the feature with no `capurl` is quarantined and the other 2,199 features on
the same response are still returned.

**3. Refuse to assume a timezone.**

```python
if parsed.tzinfo is None:
    raise ValueError(f"SWIC {field} {value!r} has no UTC offset; refusing to assume one")
```

`astimezone()` on a naive datetime silently assumes the *server's* local time. The
same deployment would produce different hazard timestamps in Lagos and London.

**Exhaustiveness is derived, not remembered.**

```python
structural = ("onset", "expires", "headline", "description")
unavailable = sorted(
    set(structural) | {k for k, v in fields.items() if v is None}
)
```

`structural` names what this feed never supplies; the set comprehension catches
everything that came back `None` for any other reason. Add a field to the schema and
it is covered automatically. Hand-maintained lists drift; this one cannot.

## query.py — one line carries the safety property

```python
partial = any((not s.ok) or s.truncated or s.invalid_count > 0 for s in statuses)
```

Failed, truncated, **or** carrying quarantined records. All three mean the answer
must not be presented as complete — a source that quarantined records is `ok=True`
(it answered) but the answer is short exactly the records that failed to parse.

```python
source_id=getattr(adapter, "source_id", "unknown")
```

The failure handler cannot itself fail on an adapter so broken it lacks a
`source_id`. Handling failure with code that can fail is how a degraded service
becomes a dead one.

## dedupe.py — reports, never edits

```python
def event_key(alert: NormalisedAlert) -> str | None: ...
def group_duplicates(alerts: list[NormalisedAlert]) -> list[DuplicateGroup]: ...
```

The key is `event` + `area_description`, case-folded and whitespace-collapsed
and nothing more — that pair is what actually matches SWIC's `us-noaa` slice
against a direct NWS fetch (see DATA-SOURCES.md's "Source overlap" section
and DECISIONS.md D13). A record with no `area_description` keys to `None`
and is excluded from grouping rather than matched to other unkeyable
records.

Grouping requires exact key agreement — no fuzzy or similarity matching
anywhere in this module, because wrongly grouping two distinct hazards is
the failure mode a future notifier (v0.5) cannot afford. Within a group,
`preferred_id` is the record with the most optional schema fields populated
(ties broken by `id` for determinism), and `AlertsResponse.alerts` is
untouched either way — `collect()` calls `summarise_duplicates()` purely to
populate `duplicate_groups` (and `ambiguous_duplicate_groups`) alongside the
full, unfiltered alert list.

**A key match alone is not enough to report a group.** Every member of a
candidate group must also come from a different `provenance.source_id` — a
source's own ids are authoritative about its own event distinctness, so
two records from one source sharing a key (98 separate GDACS wildfires all
keyed `wildfire|angola`, since `gdacs:country` is country-level — see
DATA-SOURCES.md) mean the key is too coarse for that source, not that the
records are duplicates. Such a candidate group is discarded entirely, and
counted in `ambiguous_duplicate_groups` rather than silently dropped
(principle 4). See DECISIONS.md D13's 18 Aug 2026 addendum for the
measurement that forced this rule.

## sources.py — coverage, not health

`/health` answers "is it working." `/sources` answers "what does this system
actually cover, and what does it miss" — a different question aimed at a
human deciding whether to rely on it for a given hazard or region, not a
monitor.

```python
class SourceSummary(BaseModel):
    source_id: str
    endpoint: str
    authorities: list[str]       # observed THIS fetch, not declared
    authority_count: int
    structural_gaps: list[str]   # from the adapter's STRUCTURAL_GAPS
    ok: bool
    alert_count: int
    latency_ms: int | None
    truncated: bool
```

**`structural_gaps` is reportable without a fetch.** Each adapter's
`structural` tuple — fields that feed structurally never supplies — was a
local inside `parse()`, invisible outside a live run. It is now a class
attribute, `STRUCTURAL_GAPS`, on all five adapters (`NwsAdapter.STRUCTURAL_GAPS`
is `()` — NWS is the one source with no structural gaps, which is correct and
meaningful, not an oversight). `parse()` still unions it with whatever came
back `None` on a given record; `/sources` reads the class attribute directly.

**`authorities` is measured, not declared.** It comes from
`{a.provenance.authority for a in <this source's alerts from this fetch>}`
— a source that is down or genuinely quiet this fetch reports an empty list,
even if it normally carries 59.

**`hazard_coverage` is the endpoint's reason to exist.**

```python
hazard_coverage: dict[str, list[str]]   # family -> source_ids that contributed
uncovered_hazards: list[str]            # families with zero contributing sources
```

Measured 18 Aug 2026: tsunami had zero contributing sources and volcano had
one, while the authority count read 59/300 and looked healthy by itself. An
authority count alone hides a missing hazard family; `hazard_coverage` does
not let that gap disappear into a healthy-looking total.

**Hazard classification is heuristic and says so.** `classify_hazard` maps
each alert's free-text `event` field to a family via an explicit keyword
table, `HAZARD_KEYWORDS`. Wording varies by authority — "THUNDERSTORMS" vs
"Heat Advisory" vs "Wildfire" — so this *will* misfile some alerts. It is
documented in the module docstring and the `/sources` endpoint docstring,
and — critically — it **never writes back to `NormalisedAlert`**; it only
builds this one summary. Extending the keyword table follows the same
evidence bar as DECISIONS.md D1: a real observed `event` string, not a guess.

**Read-only over the shared cache.** `build_sources_response()` takes an
already-collected `AlertsResponse` and never mutates it or anything
reachable from it; `/sources` calls `_collect_shared()`, the same
non-deep-copying path `/health` uses, per D9.

**CAP detail enrichment (issue #4, D16).** `parse_cap_detail()` reads a raw
CAP 1.2 file into a `CapDetail` model; `fetch_cap_detail()` fetches one,
cached by `capurl` **forever** (no TTL — the path is content-addressed, so
the same `capurl` can never resolve to different bytes). `enrich_with_detail()`
merges a `CapDetail` into a `NormalisedAlert`, filling only fields the list
view had none of (`headline`, `description`, `instruction`, `onset`,
`expires`, `geometry`), except severity/urgency/certainty: there, the CAP
file's named value wins outright over the list view's mapped one whenever
both are present, since the CAP file is the authority's own signed record.
None of this runs from `fetch()`/`parse()` — it is only reachable through
`SwicAdapter.fetch_detail()` and the `/alerts/{alert_id}/detail` route,
because fetching one CAP file per alert on every list poll would be ~2,200
requests to WMO per fetch.

## api.py — three things worth knowing

**The cache hands out deep copies.**

```python
return cached[1].model_copy(deep=True)
```

`/alerts` filters `response.alerts` in place. The mutation was safe while the object
was per-request; adding a cache made it shared, at which point one request's filter
would poison the next. A fix in one place invalidated a decision made in another —
worth remembering when adding state.

**An unknown authority is not a quiet day.**

```python
if not matching:
    response.available_authorities = sorted({a.provenance.authority for a in response.alerts})
    response.available_authorities_partial = response.partial
```

A typo returns `[]`, which in this domain reads as "that country has issued no
warnings" — a dangerous false negative. Naming the authorities that answered lets a
caller tell a typo from genuine quiet. But that list is built only from alerts this
fetch actually got back, so a source that was down during a partial fetch can drop a
perfectly real authority out of it too — `available_authorities_partial` (D7,
extended for issue #9) says explicitly when that list itself might be short, rather
than leaving a caller to separately notice `partial` was also true.

**`/alerts/{alert_id:path}/detail` uses the `:path` converter, not the
default one.** An alert id embeds a `capurl`, and a `capurl` contains `/`
(`ng-nimet-en/2026/08/17/.../a.xml`). The default single-segment converter
stops at the first `/`, which would make most real SWIC ids unroutable.
Two distinct failure modes get two distinct status codes: `404` when
`alert_id` is not in the current fetch, `502` when the id is known but its
CAP file could not be fetched or parsed — never a silently empty record for
either.

**Degraded service is visible at the status line.**

```python
if result.partial:
    return JSONResponse(status_code=503, content=body)
```

`200 OK` carrying `{"ok": false}` reads green to every standard monitor.

## mcp_server.py — same data, a different consumer

`mcp` is an optional extra (`pip install alertmux[mcp]`) — see DECISIONS.md
for why the core library does not depend on it. `mcp_server.py` is only
importable when it is installed; `tests/test_mcp_server.py` guards with
`pytest.importorskip("mcp")` so the core suite is unaffected either way.

It builds an `MCPServer` (from `mcp.server.mcpserver` — not `FastMCP`,
which does not exist in `mcp` 2.0) and registers four tools:
`list_alerts`, `list_sources`, `get_hazard_coverage`, `find_duplicates`.
Every tool calls `alertmux.api._collect_cached()` — the same TTL-cached
path `/alerts`, `/health` and `/sources` already share — rather than
calling adapters directly, so an MCP client never causes a second,
independent hammering of WMO/USGS/NWS/GDACS/EONET alongside the HTTP API.

**Every response model carries `DISCLAIMER` as a field**, not just a tool
description. This matters more here than over HTTP: an HTTP consumer is
code that can be written once to check `partial` before acting, but an MCP
consumer is an LLM that will paraphrase the result for an end user. If the
disclaimer and the partiality/truncation flags are not *in the data*, a
model has nothing to relay — it will confidently summarise a degraded or
truncated fetch as complete. `list_alerts` therefore reports `partial`
(some source failed or was truncated in this fetch) and `truncated`
(this call's own `limit` cut off matching results) as two separate
booleans, on the same reasoning as `query.py`'s `partial` vs `truncated`
split.

`get_hazard_coverage` exists as its own tool, not folded into
`list_sources`, because it answers the specific question this project
considers dangerous to get wrong by omission: whether "no alerts for X"
means a quiet day or means no source covers hazard family X at all (e.g.
no tsunami source). Surfacing it as a dedicated, prominently-described
tool makes it something a model is likely to check before asserting an
absence, rather than a field buried in a larger coverage report.

## notify/ — the self-hosted notifier

Deliberately isolated from `api.py`: no shared module, no imports in
either direction beyond `notify/cli.py` and `notify/runner.py` calling
`alertmux.query.collect()` and `alertmux.adapters.default_adapters()`,
the same read-only entry points `api.py` itself uses. This is a process
concern, not an HTTP surface — it has no endpoints, and touching
`api.py` was explicitly out of scope for this build (three outside
contributors have open PRs rewriting its cache).

```
config.py ──> rules.py (pure) ──┐
state.py ────────────────────────┼──> runner.py ──> cli.py
delivery.py ─────────────────────┘
```

- **`config.py`** loads `[smtp]` / `[state]` / `[[rules]]` from a TOML
  file via stdlib `tomllib`, with `ALERTMUX_SMTP_*` environment variables
  overriding the `[smtp]` block so a committed config file need not carry
  a password. `SmtpConfig.password` is a pydantic `SecretStr` specifically
  so an accidental `str(config)`/`repr(config)`/log call cannot print it;
  `ConfigError` messages are built only from field names and validation
  reasons, never the raw parsed dict, so a validation failure elsewhere in
  the file cannot leak a password sitting next to it.

- **`rules.py`** is pure: `evaluate_rule(rule, alerts)` takes data, returns
  data, touches no filesystem, network or clock. This is what makes the
  unmapped-severity design flaw testable in isolation — see below.

- **`state.py`**'s `StateStore` is a flat JSON file keyed by alert id
  (D2: stable, derived from `capurl`), so "already notified" is a
  membership check. Writes go through a temp file plus `os.replace` so a
  crash mid-write cannot corrupt the file into something that reads as
  "notify everything again."

- **`delivery.py`** wraps stdlib `smtplib` only — no new dependency, no
  default sender (D11). `build_alert_message`/`build_digest_message`
  assemble the verbatim relay fields (headline, description, authority,
  source URL, retrieved_at, onset, expires, `DISCLAIMER`) into an
  `EmailMessage`; nothing here reformats hazard text. `SmtpSender.send`
  never swallows an SMTP exception — it raises `DeliveryError`, built only
  from host/port and the underlying exception's own type/message, never
  from `SmtpConfig` itself.

- **`runner.py`**'s `run_once` is one poll cycle over an
  already-collected `AlertsResponse`: drop expired alerts (an *unknown*
  expiry is kept, never treated as expired — the same safe-direction
  default as the severity rule), collapse each cross-source duplicate
  group down to its `preferred_id` record only (D13 — so a rule matching
  both SWIC's and NWS's copy of one NOAA warning fires once), evaluate
  every rule, cap new alerts at `max_per_run`, optionally batch into one
  digest email, and persist state only for alerts actually delivered. An
  SMTP failure is logged at ERROR and left out of state on purpose, so
  the next run retries it instead of the alert being silently lost.

- **`cli.py`** is the thin `alertmux-notify` entry point: load config,
  prune state, `collect()`, `run_once()`, print a summary, exit non-zero
  on any delivery failure or invalid config.

### The unmapped-severity design flaw, and how this module refuses it

A rule reading "notify on Severe or above" cannot be evaluated against an
alert whose `severity` is `None` — which is every GDACS alert (D1: its
`alertlevel` is an impact score, never mapped to CAP severity) and every
tsunami.gov bulletin (D17: its bulletin category is deliberately never
mapped). Silently treating "cannot evaluate" as "does not match" would
mean a life-safety notifier configured for "Severe or above" delivers
zero GDACS earthquakes and zero tsunami warnings while the operator
believes they are covered.

`evaluate_rule` in `rules.py` never takes that path. For every alert a
severity-threshold rule cannot rank (a `None` severity, or the legal-but-
unrankable CAP value `"Unknown"` NWS can emit), it increments
`RuleMatch.unevaluable_count` and records the source, regardless of
whether the alert ends up included or excluded. `RuleConfig.
include_unmapped_severity` defaults to `True`: absent an explicit
operator opt-out, an unrankable alert is still delivered — a missed
hazard is the unrecoverable failure direction, an extra email is not, the
same asymmetry D1 and D13 already apply elsewhere in this codebase.
`warn_severity_rules_against_unmapped_sources` runs at the start of every
`run_once` call (dry-run included) and produces one message per
(rule, source) pair where a severity rule touches a source that never
contributes a rankable severity in that fetch — naming the source
explicitly, so this is a printed warning on every run, not a fact the
operator has to go looking for. See DECISIONS.md's notifier entry for the
measured counts that motivated this and the full design reasoning.

## dashboard/ — the operational dashboard

Local, read-only, single operator. Entirely separate from `api.py` — its
own FastAPI app, its own console script (`alertmux-dashboard`), its own
cache — so the two open PRs rewriting `api.py`'s cache have nothing here
to conflict with. See DECISIONS.md for why this separation is a hard
constraint, not a convenience.

```
query.collect() ──> collector.py (TTL cache + health memory) ──┐
volume.py (JSONL) <──────────────────────────────────────────┤
sources.py, registry.py ───────────────────────────────────────┼──> app.py ──> page.py
notify/runlog.py (JSONL, read only) ───────────────────────────┘
```

- **`collector.py`**'s `DashboardCollector` wraps `alertmux.query.collect`
  behind the same TTL policy `api.py` uses (60s complete, 10s partial),
  but as its own instance — no shared cache, no import of `api.py`
  internals. On every *fresh* fetch (never a cache hit) it also updates
  `SourceHealth` per source: `last_ok_at` and `consecutive_failures`,
  neither of which any existing module tracks, since `query.py` and
  `sources.py` both only ever describe one fetch at a time.

- **`volume.py`**'s `VolumeRecorder` is an append-only JSONL file, one
  line per fresh fetch (timestamp, per-source ok/alert_count/latency,
  totals, `partial`). `prune()` keeps it bounded; `recording_started_at()`
  returns `None` for an empty file specifically so the page can render
  "no data yet" rather than a chart that reads as "zero alerts always."

- **`app.py`** exposes exactly three GET routes: `/` (the HTML page),
  `/api/summary` (source health, coverage, volume history, notification
  log, registry freshness — one call for everything the page's periodic
  poll needs), and `/api/alerts` (filterable by authority/severity/event
  for the live-alerts panel). No route mutates anything; a test asserts
  the app exposes no non-GET routes.

- **`page.py`** renders one self-contained HTML string: inline CSS and
  JS, no external assets, no build step. The relay disclaimer and the
  hazard-classification heuristic caveat are written into the static
  HTML itself, not only fetched at runtime, so both are present even
  before the page's first JS poll completes.

- **`cli.py`** is the `alertmux-dashboard` entry point: parse `--host` /
  `--port` / `--volume-log` / `--run-log`, point the app's persistence at
  those paths, run uvicorn.

## What is deliberately absent

`api.py` gained no notifier-related endpoints in this build (see
`notify/`'s section above for why) — SMTP failure surfacing on `/health`
remains a follow-up issue, not a design rejection; the dashboard half of
that gap is closed instead (see `dashboard/` above). No accounts, no
database, no frontend build step. alertmux reads official feeds,
normalises them, and (as of the notifier) can relay matching alerts to an
operator's own inbox; it still never originates a warning. The boundary
is legal as well as architectural — see `DECISIONS.md`.
