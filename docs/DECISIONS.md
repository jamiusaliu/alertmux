# Decisions

Why alertmux is the way it is. Each entry records the decision, the reasoning, and
what it would cost to be wrong — so a future contributor can overturn it with
evidence rather than re-argue it from scratch.

> **Keeping this current:** add an entry whenever you make a choice someone could
> reasonably make differently, especially one that will look like a mistake to a
> newcomer. Never delete an entry — supersede it, with the date and the evidence
> that changed your mind. The point of this file is that decisions outlive the
> people who made them.

## The four principles

Everything below serves one of these. If a change violates one, the change is wrong,
however convenient.

1. **It relays, it never issues.** Alert text passes through verbatim. The system
   never authors, rewords, or infers hazard content.
2. **Nothing is ever inferred.** A field a source does not supply is `None` and named
   in `unavailable_fields`; a field the source did supply but that alertmux declines
   to translate is `None` and named in `unmapped_fields` instead (see D18). Never
   defaulted, never guessed, and never conflated with each other.
3. **Only verified severity codes are mapped.** Codes never observed in real CAP data
   stay untranslated.
4. **Silent partial success is a bug.** Any incomplete answer is labelled.

---

## D1 — The severity tables are deliberately incomplete

**Decision.** `URGENCY` and `CERTAINTY` in `adapters/swic.py` have no key `1`; no
table has a key `0`.

**Why.** The mapping was established by fetching seven real signed CAP files and
comparing their `cap:severity`/`cap:urgency`/`cap:certainty` against the integer
codes. Codes `1` (urgency, certainty) and `0` (all three) never appeared in that
sample. CAP's own ordinal scale suggests `1` should be `Past` and `Unlikely`, and the
pattern is unmistakable — but a pattern is not evidence.

**This will look like an oversight.** It is the single most likely "helpful fix" in
the codebase. It is guarded by `test_unverified_codes_stay_untranslated`.

**What would justify changing it.** A real CAP file from
`https://severeweather.wmo.int/v2/cap-alerts/<capurl>` whose WFS record carries the
code and whose XML shows the corresponding name. Attach it to the PR. Then add the
key and a test.

**Cost of being wrong.** Mapping a code incorrectly means a consumer acts on a
severity the authority never stated — an alert treated as minor when it is extreme,
or an immediate warning read as past. An unverified code costs a null and a name in
`unmapped_fields` (D18) — the code was supplied and refused, not absent — which is
recoverable. The asymmetry is the whole argument.

## D2 — Alert ids derive from `capurl`, not the server's feature id

**Decision.** `id = f"{source_id}:{capurl}"`. A missing `capurl` raises.

**Why.** GeoServer's synthetic feature ids embed a *request* timestamp, so the same
alert returns a different id on every fetch. Verified: the same NiMet warning fetched
twice seconds apart gave ids ending `_2d5b` and `_2d5c` with an identical `capurl`.

**Cost of being wrong.** Deduplication is built on this. An unstable id means every
poll looks like a fresh batch — for a future notifier, that is a mailbomb during
exactly the severe-weather event people depend on it for.

**Note.** The test that originally guarded id stability was
`parse(FIXTURE)[0].id == parse(FIXTURE)[0].id` — parsing the same static dict twice.
It could never fail, and it gave everyone false confidence on precisely this point.
Its replacement asserts the id is a function of `capurl` and independent of the
server's fid.

## D3 — A malformed record is quarantined, not fatal

**Decision.** A record that cannot be parsed — a missing `capurl`, an unparseable
authority prefix, a naive timestamp, a missing USGS `type` or `id`, an unknown GDACS
`eventtype` — is **skipped**, counted into `SourceStatus.invalid_count`, and forces
`partial=true`. The valid records in the same response are still delivered.

**Why.** Failing loudly beats quietly inventing, but failing *entirely* was too
blunt. Both alternatives were worse: substituting a placeholder violates principle 2
(nothing inferred), and dropping the record silently violates principle 4 (no silent
partial success). Quarantine satisfies both — nothing is invented, and the
incompleteness is stated.

**The reason is preserved.** `SourceStatus.invalid_samples` carries the exception
type and message for the first few quarantined records, capped so a mass failure
cannot produce a megabyte of errors. An operator can see *why* records are being
dropped, which is what makes a shape change in an upstream feed diagnosable rather
than mysterious.

**Envelope errors are still fatal, deliberately.** A response that is not valid
GeoJSON or RSS at all — the `_require_feature_collection` checks — still returns
`ok=false` with no alerts. The distinction is the point: a malformed *record* means
one record is broken; a malformed *envelope* means the source is broken. Collapsing
the two would let a GeoServer error page parse as "zero hazards worldwide", which is
the failure D-numbered elsewhere as the worst this system can produce.

**Cost if wrong.** A record that *should* have failed the whole fetch now passes
quietly into a count. That is mitigated by `partial=true` and the sample messages —
the response never claims completeness it does not have.

### Superseded — the v0.1 behaviour, kept for the record

Until 18 Aug 2026 a single malformed record aborted the entire fetch: one bad record
from one authority discarded 2,200 good warnings from 53 other services, and the
60-second cache pinned that blackout. It was tolerable only because the failure was
loud (`ok=false`, `partial=true`, HTTP 503) — the operator knew they were seeing
nothing rather than believing an incomplete list was complete. It was still the
wrong trade, and issue #1 tracked replacing it.

## D4 — `truncated` is separate from `ok`, and both make a response partial

**Decision.** `FetchResult.truncated` and `SourceStatus.truncated`, with
`partial = any((not s.ok) or s.truncated for s in statuses)`.

**Why.** A truncated fetch is a *correct* answer that is *incomplete*. Folding it
into `ok` would lose that distinction; leaving it out of `partial` would let the API
report complete data while dropping alerts.

**Comparison alone is not enough.** Detection by `numberMatched > numberReturned`
requires both to be integers. GeoServer can emit `"numberMatched": "unknown"`, in
which case that test fails **open** — defaulting to "complete" exactly when the
server declines to say. The belt-and-braces rule is therefore also applied:
`returned >= max_features` sets `truncated` on its own, so a page filled to the
cap is never reported as complete no matter what the server claimed it matched.

**Superseded 18 Aug 2026 (issue #3) — real `startIndex` pagination shipped.**
`fetch()` now loops on `startIndex`, requesting a further page whenever the
current page came back filled to `maxFeatures` (the belt-and-braces rule
above, unchanged) or `numberMatched` says the cumulative offset is still
short of the total. The loop is bounded by `max_pages` (default 20): hitting
that ceiling before the server is confirmed exhausted leaves `truncated`
True, exactly as the old "just label it" behaviour did — the difference is
that a fetch under the ceiling now actually retrieves the extra warnings
instead of only announcing that some were missing. The loop also stops
early, with `truncated` True, if a page contributes zero records not
already seen: a paginating GeoServer can theoretically reorder results
between requests, and no forward progress means no amount of further
requesting will help. Records are deduplicated across pages by id (derived
from `capurl`, D2) rather than trusted to arrive exactly once; a collapsed
duplicate is counted in `FetchResult.duplicate_count`/`SourceStatus.duplicate_count`,
never silently dropped. Per-record quarantine (D3) applies independently on
every page and accumulates into the same `invalid_count`/`invalid_samples`.
`FetchResult.returned` changed meaning accordingly: it now reports the
number of distinct valid alerts assembled across all pages, not one page's
raw `numberReturned` — the latter stopped being a single number once there
could be more than one page.

**`sortBy` is mandatory, not decoration — found live, not in any unit
test.** WFS 1.1.0 does not mandate a feature order, so `startIndex` is only
well-defined paired with a sort key; this GeoServer enforces that stricter
than a graceful fallback would suggest. Verified 18 Aug 2026: a request
carrying `startIndex` with no `sortBy` gets back HTTP 200 with the literal
body `Err\nErr\nErr\nErr\nErr\nErr\n` — not JSON, not a WFS
`ExceptionReport`, nothing `_require_feature_collection` was written to
catch as a *envelope* failure (it does raise, correctly, just not for the
reason anyone would guess from the message). Every mocked unit test for
this feature passed regardless, because respx returns whatever payload the
test wrote; only the live smoke suite could have caught this, and initially
didn't either, because the first live run after shipping pagination hit
this exact failure. `build_params` now always sends `sortBy=capurl` —
`capurl` is already the identity field (D2), unique and stable, so sorting
by it costs nothing and gives deterministic page boundaries as a side
effect (confirmed live: two consecutive pages with `sortBy=capurl` returned
disjoint, alphabetically contiguous slices with zero overlap). **The
lesson, restated from the "things we got wrong" section below because it
bit this exact feature during its own development:** a fixture proves the
parsing logic works on the shape you imagined the server would return: it
cannot prove the server accepts the request you imagined it would accept.

**What was true in v0.1, kept for the record.** v0.1 only *labelled*
truncation: it requested a single page capped at `maxFeatures`, and if the
server had more warnings in force than it returned, the response still set
`truncated` — but the extra warnings were never fetched. That was tolerable
while live counts (~2,100–2,300) sat safely under the 3,000 cap, but a
Northern-Hemisphere severe-weather day plus a US NOAA outbreak was always
going to exceed it, and dropping alerts is least acceptable at exactly that
moment. Real pagination is the fix; this entry's original text is preserved
above this note as the reasoning that made the gap visible before it was
closed.

## D5 — The disclaimer lives in response bodies, not only in the OpenAPI description

**Decision.** `DISCLAIMER` is a field on `AlertsResponse` and a key in `/health`'s
body.

**Why.** A consumer reading JSON never sees the OpenAPI description, so a
documentation-only disclaimer reaches exactly the wrong audience. Putting it on the
model means the MCP server and notifier inherit it automatically rather than each
having to remember.

## D6 — `sources` and `partial` describe the fetch, not the filter

**Decision.** With `?authority=` applied, `alerts` is filtered but `sources[]` and
`partial` still describe the whole unfiltered fetch. So `alert_count: 1956` can sit
beside a single alert.

**Why.** Source health is a property of the fetch. A caller filtering to `ng-nimet`
still needs to know USGS is down. Filtering `sources` to match would falsely imply an
authority-scoped failure.

**Mitigation.** Documented in the `/alerts` docstring and the README, because it is
genuinely confusing on first encounter.

## D7 — An unknown `?authority=` names the authorities that answered

**Decision.** When the filter matches nothing, the response carries
`available_authorities`.

**Why.** A typo and a genuinely quiet day both return `[]`. In this domain that
ambiguity is a dangerous false negative — "no warnings for that country" is a very
different statement from "you misspelled it."

**Extended 18 Aug 2026 (issue #9) — the list itself can be short for a
third reason, and that must be said too.** `available_authorities` is
built only from `response.alerts` — the alerts *this fetch actually
returned*. During a partial fetch (a source down, truncated, or
quarantining records — see D3/D4), an authority whose only source
failed silently drops out of that list along with everything else from
that source. `partial: true` was already present alongside it, but it
warns that *something* is incomplete without saying the authority list
itself is one of the things that is short — a caller checking
`available_authorities` for a specific authority has no reason to also
go check `partial` unless told to.

`AlertsResponse.available_authorities_partial: bool | None` closes
that gap: set to `response.partial` whenever `available_authorities`
is populated, and left `None` the rest of the time (there is nothing
for it to qualify when the filter matched). A caller now gets three
distinct answers instead of two: the filter matched (`alerts`
non-empty); it matched nothing on a complete fetch (`available_authorities`
set, `available_authorities_partial: false` — the authority is
genuinely unrecognised or genuinely quiet); or it matched nothing on
an incomplete fetch (`available_authorities_partial: true` — the
authority may be entirely valid and simply missing because its source
was unreachable when this fetch ran).

**Why not use the WMO register (D15) to assert the authority is
real.** D15 already ruled this out for a stronger reason than
convenience: the register joins at country level only, because WMO's
`raa:authorityAbbrev` and SWIC's `capurl` abbreviation independently
name the same real-world agency differently (Nigeria: `nma` vs
`nimet`). Using the register here would mean asserting "yes, this
specific authority slug is registered" for a slug the register cannot
actually validate — exactly the false certainty D15 exists to refuse,
now in a place where the false certainty would look like it was fixing
issue #9 instead of repeating D15's mistake. `available_authorities_partial`
states only what this fetch can actually prove: that its own authority
list is, or is not, complete.

**Cost of being wrong.** Before this flag, a caller who filtered on a
known-good authority during a partial fetch would see `[]` plus a
short `available_authorities` that did not include it, and had no
field-level signal that the omission might be the fetch's fault rather
than the authority's — in a hazard system, reading that as "this
authority does not exist" is the dangerous direction to be wrong in.
Guarded by `tests/test_api.py`'s
`test_unknown_authority_on_a_partial_fetch_is_flagged_partial` and its
complete-fetch counterpart.

## D8 — `/health` returns 503 when degraded

**Decision.** HTTP 503 with the same JSON body whenever `partial` is true.

**Why.** `200 OK` carrying `{"ok": false}` reads green to every standard monitor,
load balancer, and uptime checker, all of which key on the status code. For a system
whose worst failure mode is a silently-broken feed, the transport signal must agree
with the body.

## D9 — The TTL cache hands out deep copies

**Decision.** `_collect_cached()` returns `model_copy(deep=True)` on both hit and
miss.

**Why.** `/alerts` filters `response.alerts` in place. That mutation was safe while
the object was per-request; adding a cache made it shared, at which point one
request's filter would poison the next.

**Worth remembering generally:** the cache did not break the filter — it invalidated
the *reasoning* that made the filter safe. Adding shared state re-opens every
decision that assumed there was none.

## D10 — Licence is MIT, and no code derives from listmonk or Ushahidi

**Decision.** MIT.

**Why.** The intended audience — NGOs, small agencies, local government — is exactly
the audience copyleft deters. Nothing here derives from `listmonk` (AGPL-3.0) or
`ushahidi/platform` (`NOASSERTION`, i.e. no grant of rights to rely on), so the
licence was freely chosen. Reading a project to learn its architecture is
unrestricted; copying its code carries obligations.

## D11 — v0.1 has no notifier, no alert issuing, no accounts

**Decision.** Out of scope, and the README says so.

**Why.** The legal exposure in this domain — false-alert liability, telecom consent
rules, and national laws on false emergency messaging — attaches to *originating*
warnings and to *mass-notifying* third parties who did not ask. It does not attach to
relaying official feeds with provenance intact.

A future self-hosted notifier, where the operator supplies their own SMTP and
configures their own subscriptions for themselves, stays on the safe side of that
line. A hosted service mass-mailing strangers does not. That boundary is a design
constraint, not a scoping convenience.

## D12 — EONET carries only the latest geometry, not the whole track

**Decision.** `adapters/eonet.py` takes the most recent entry of an event's
`geometries` list (sorted defensively by `date`) and discards the rest. `sent`
is set from that same geometry's `date`, not any other timestamp on the event.

**Why.** `NormalisedAlert.geometry` holds a single GeoJSON dict — the same
shape every other adapter fills — so the real choice was "latest point" versus
"bundle the whole track into a `GeometryCollection` unique to this source."
Latest wins: it answers "where is this event now," which is what a relay
consumer expects from a single geometry field, and it keeps the field
literally interoperable with every other adapter instead of introducing a
one-off shape. A consumer that wants the full accumulated track already has
it — `provenance.raw_reference` is the event's own EONET API page, which
serves the complete geometry history.

**Cost of being wrong.** A cyclone's latest point is a snapshot, not a path —
a consumer inferring direction of travel from one alert gets nothing; they
would need to diff successive polls. That is an accepted limitation of
"observations, not warnings," not a defect: EONET itself makes no claim about
where an event is *going*, only where it has been *seen*.

**What would justify changing it.** A consumer need that specifically
requires the full track in one alert (e.g., rendering a storm path) — at which
point a `GeometryCollection` becomes worth the schema inconsistency it costs
every other adapter's assumption of a single Point/Polygon.

## D13 — Duplicate detection reports, it never merges or drops

**Decision.** `dedupe.py` groups alerts that share an exact, normalised
`event` + `area_description` key and names the richest record in each group
as `preferred_id`. It never removes an alert from `AlertsResponse.alerts`
and never combines two records into one. Matching is exact-only: no edit
distance, no token overlap, no similarity threshold anywhere in the module.

**Why.** Measured 18 Aug 2026: SWIC's `us-noaa` slice and a direct NWS fetch
overlap completely — 133 of SWIC's 133 distinct (event, area_description)
pairs also appear in NWS's 148, and NWS carries 32 source fields against
SWIC's 9. That overlap is real and worth surfacing. But merging would mean
choosing whose wording a consumer sees, which is authoring hazard content by
another name — forbidden by principle 1. And approximate matching trades a
recoverable cost (a redundant record) for an unrecoverable one: v0.5's
notifier will read this module's output, and wrongly grouping two distinct
hazards there could suppress a real warning. Preferring false negatives is
therefore not caution for its own sake — it is the same asymmetry D1 already
applies to severity codes, applied here to identity.

`preferred_id` ranks by how many optional schema fields are populated — the
inverse of `unavailable_fields`, so it is a count anyone can verify from the
response, not a judgement about which authority is more trustworthy. Ties
break on `id` so the same input always produces the same output.

**Cost of being wrong.** Missing a duplicate (e.g. wording differs enough
that a future refinement of the key would catch it) costs a redundant
record shown twice — annoying, never dangerous. Wrongly grouping two
distinct hazards would let a consumer treat two different warnings as one,
which for a future notifier means a suppressed alert during exactly the
event people depend on it for. The exact-match-only rule exists to keep
that second failure mode unreachable.

**What would justify changing it.** A demonstrated need for merging (not
just reporting) would require a separate design decision, not an extension
of this module — it changes principle 1's guarantee and needs its own
sign-off. A demonstrated case where exact key matching misses real
duplicates that a *conservative, specific, documented* extra normalisation
step would catch (not a general similarity threshold) could extend
`_normalise`, with the same evidence bar as D1: a real observed pair,
attached to the PR.

**Addendum, 18 Aug 2026 — a source's own ids are authoritative about its
own event distinctness.** The rule above (exact `event` + `area_description`
match) shipped and immediately produced a false-positive catastrophe:
against live data it formed a "group of 98" keyed `wildfire|angola` — 98
*separate* GDACS wildfire events, each carrying its own distinct
`gdacs:eventid`, spanning ten days, collapsed into one group purely
because GDACS's `area_description` is country-level (`"Angola"`, nothing
finer — see DATA-SOURCES.md). The same defect produced groups of 49
(`wildfire|the democratic republic of congo`) and 32 (`wildfire|brazil`).
When GDACS assigns two records different event ids, GDACS is *stating*
they are different events, and no cross-source key this module invents
gets to overrule that.

**The fix.** A candidate group (2+ records sharing an `event_key`) is
only reported as a `DuplicateGroup` when every member comes from a
*different* `provenance.source_id`. If any source contributes two or
more records to a candidate group, the key is too coarse for that
source's own identity guarantees and the **entire group is discarded**
— not narrowed, not partially kept. Two candidate rules were measured
against the same live fetch: a loose rule (more than one distinct
source present) produced 133 groups, 17 of which still contained
multiple records from a single source and were therefore exactly this
failure mode in miniature; the strict rule (every member a different
source) produced 116 groups, all clean pairs, largest group size 2 —
matching the verified SWIC↔NWS overlap this module was built to
surface. 116 confident pairs beats 133 with 17 questionable ones: the
same "prefer false negatives over false positives" asymmetry this
entry already argues for, applied to its own edge case.

**Rejected groups are not silently dropped (principle 4).**
`AlertsResponse.ambiguous_duplicate_groups` counts how many candidate
groups were found but discarded this way, so an operator can see that
duplicates were suspected but could not be confidently paired — rather
than the count simply vanishing. See `query.py` and
`dedupe.summarise_duplicates`.

**What would re-open this.** Evidence that the strict rule is
discarding real cross-source duplicates in bulk (not the rare
coincidence) would be the bar — the same evidence standard as the rest
of this entry.

## D14 — `mcp` is an optional dependency, and its disclaimer/partiality flags matter more than over HTTP

**Decision.** `mcp` lives in `[project.optional-dependencies]` under the
`mcp` extra, not in `dependencies`. `mcp_server.py` is only importable
when it is installed, and `tests/test_mcp_server.py` guards every test
with `pytest.importorskip("mcp")` so the core suite is unaffected either
way.

**Why it is optional.** alertmux's core runtime is pydantic/httpx/fastapi/
uvicorn, and its value is five small adapters people can copy. `mcp`
2.0.0 drags in a second, distinct HTTP client (`httpx2` — not the
`httpx` this project already uses), `cryptography`, `opentelemetry-api`,
`pyjwt`, `sse-starlette` and `python-multipart`. None of that is needed
to fetch and normalise alerts; forcing it on every installer to serve the
minority who want an MCP client would violate the same "stay light"
reasoning that keeps the core dependency list to four packages.

**Why the disclaimer and partiality flags matter more over MCP than
HTTP.** An HTTP consumer of `/alerts` is code: it can be written once,
correctly, to check `partial` before acting, and a human reading the raw
JSON sees `disclaimer` as text. An MCP consumer is an LLM that will
*paraphrase* the tool result for an end user — if `partial`, `truncated`
and the disclaimer are not fields on the structured response itself, the
model has nothing concrete to relay and will confidently summarise a
degraded or truncated fetch as complete, which is exactly principle 4's
failure mode, now with an LLM's fluency behind it. This is why every
`mcp_server.py` tool response model carries `disclaimer: str = DISCLAIMER`
directly (not only in a tool `description=`, which a model deciding
whether to call a tool reads, but not necessarily on every subsequent
turn), and why `list_alerts` reports `partial` (the underlying fetch) and
`truncated` (this call's own `limit`) as two distinct booleans rather than
folding truncation into a silent slice of `alerts`.

**Cost of being wrong.** Making `mcp` a hard dependency would tax every
installer — including the ones already copying single adapters, per the
README — with packages irrelevant to their use case. Omitting the
disclaimer/partiality fields from the MCP response models would not
break any test at the HTTP layer, since they are two separate surfaces;
it would only show up as a Claude conversation confidently reporting "no
alerts" when a source was actually down, which is much harder to catch
than a failing assertion.

## D15 — `/authorities` joins the WMO register to alertmux's own coverage at country level, never authority level

**Decision.** `registry.py` computes `countries_covered` /
`countries_uncovered` by mapping each register entry's ISO 3166-1 alpha-3
`iso:countrycode` to alpha-2 through an embedded table, then comparing
against the alpha-2 prefix of authorities that returned an alert in the
current fetch. It does **not** attempt to prove a register entry corresponds
to a specific alertmux source by reconstructing an authority slug, except as
a best-effort, exact-match-only `matched_authority` field that is expected
to stay null for most entries.

**Why.** Investigated 18 Aug 2026 (see docs/DATA-SOURCES.md's join-problem
section). Two independent obstacles rule out an authority-level join:

1. The register's alpha-3 country codes have no alpha-2 mapping in the feed,
   and the mapping cannot be derived by truncation (`ZAF` ≠ `za`'s first two
   letters of anything derivable from `ZAF` itself; `DEU` → `de`, `GBR` →
   `gb` — none of these follow a rule). This is solved by an embedded ISO
   3166-1 table, not a design compromise.
2. `raa:authorityAbbrev` disagrees with the abbreviation alertmux's own
   sources use for the identical authority. WMO records Nigeria's agency as
   `nma`; SWIC's `capurl` calls it `nimet`. `us-noaa` is the one case where
   the two abbreviations happen to agree — not evidence the join works in
   general, but the exact coincidence that would make a lazier
   implementation look correct in testing and wrong in production.

Country is a reliable join key once alpha-3 is mapped to alpha-2; authority
identity is not, because two different organisations (WMO and SWIC)
independently invented different short names for the same real-world agency
and neither is wrong — they simply never agreed on one.

**Never claim an authority-level match you cannot prove (principle 2,
extended).** A register entry alertmux cannot link to a carried source is
reported as **unmatched** (`matched_authority: null`), which is a distinct
claim from "uncovered." Nigeria's WMO entry is unmatched even when
`ng-nimet` alerts are present and Nigeria itself is `covered` — the country
has warnings; the *specific WMO listing* simply cannot be proven to be the
*specific* source alertmux carries, only that some source for that country
exists. Collapsing "unmatched" into "uncovered" would misreport countries
alertmux actually covers as gaps, and collapsing "unmatched" into "matched"
would assert an authority-level link this data cannot support — both
failure directions this project exists to avoid (principle 4).

**Cost of being wrong.** Guessing an authority-level match (e.g. fuzzy
string matching WMO's abbreviation against alertmux's) risks silently
linking two different agencies that happen to have similar short names —
worse than the honest gap, because it looks like verified coverage. An
unmatched entry costs nothing but an accurate `null`; a wrongly matched one
would misinform anyone deciding whether a given register authority is
already covered.

**What would justify changing it.** A published, authoritative WMO-to-SWIC
(or WMO-to-alertmux) authority identifier crosswalk — not a heuristic this
project invents, since inventing one is exactly the failure mode this
decision refuses.

## D16 — CAP detail is opt-in per alert, cached forever, and its named
severity/urgency/certainty outrank the list view's integer codes

**Decision.** `adapters/swic.py` adds `fetch_cap_detail()` /
`SwicAdapter.fetch_detail()`, fetching and parsing one alert's raw CAP 1.2
file on explicit request only — never from `fetch()` or `parse()` — and a
new `GET /alerts/{alert_id:path}/detail` route that resolves the alert's
`provenance.raw_reference` (its `capurl`) and returns the merged record via
`enrich_with_detail()`. Where the CAP file states a named `severity`,
`urgency` or `certainty`, that value **replaces** the list view's
integer-code mapping in the enriched record; the raw integer stays
untouched in `source_severity`/`source_urgency`/`source_certainty` either
way.

**Why fetching stays opt-in.** With ~2,200 alerts in force, fetching one CAP
file per alert on every `/alerts` poll would be ~2,200 requests to WMO per
fetch — far too expensive for a list endpoint (issue #4, same reasoning the
module docstring already gave for why v0.1 never did this inline). The
detail route costs exactly one extra request, made only when a caller
actually wants one alert's full record.

**Why the cache has no TTL.** `capurl` is content-addressed — verified 17
Aug 2026 (see DATA-SOURCES.md): the path itself embeds a hash of the file's
own content, so the same `capurl` can never resolve to different bytes once
published. A TTL exists to bound how long a *possibly-changed* value is
served as fresh; there is nothing here that can change, so `fetch_cap_detail`
caches unconditionally rather than pretending there's a staleness window to
manage. This is a different cache discipline from `registry.py`'s (D15's
neighbour, 24h TTL with reported age) and from `api.py`'s 60s `/alerts`
cache — both of those front data that *does* change.

**Why the CAP-named values outrank the list view's mapped ones.** The CAP
file is the authority's own signed record — the same document the list
view's `s`/`u`/`c` integers were themselves confirmed against (D1). When
both are present, the CAP file is the more direct statement, not a second
opinion to weigh against the first. This does not relax D1: the integer
table still maps only verified codes, and an unmapped integer still yields
`None` on the list view — CAP detail enrichment fills that gap when
`fetch_detail` is actually called, it does not change what `/alerts` alone
can state.

**Why `fetch_detail` raises rather than returning `None` on failure.** A
`None` return is ambiguous between "the CAP file has nothing to report"
(which cannot happen — `parse_cap_detail` requires at least one `<info>`
block or raises) and "the fetch or parse failed." Principle 4 forbids
exactly that ambiguity, so failure is a raised `CapDetailError`, and the API
route turns it into a distinct `502` — separate from the `404` an unknown
`alert_id` gets — rather than any code path returning an emptied-out
record that would read as "no detail exists."

**Why `{alert_id:path}`, not the plain string converter.** An alert id is
`f"{source_id}:{capurl}"` and `capurl` itself contains `/`
(`ng-nimet-en/2026/08/17/14/50/16-<hash>.xml`). FastAPI's default path
converter stops at the first `/`, which would make most real SWIC alert ids
unroutable through a plain `{alert_id}` segment.

**Cost of being wrong.** Caching a CAP file forever on a wrong assumption
about content-addressing would serve a stale record indefinitely with no
way to detect it — the evidence bar above (verified path structure, not
assumed) exists specifically to guard against that. Letting the CAP-named
severity silently disagree with the list view's mapped value without
documenting the precedence would leave a future reader unable to tell which
was chosen or why; this entry is that record.

**What would justify changing it.** Evidence that a `capurl` has ever
resolved to different content on a re-fetch would break the "cache forever"
premise and require a TTL. Evidence that the CAP file's own
severity/urgency/certainty disagree with reality more often than the list
view's mapped codes would reopen which one should win — none observed as of
18 Aug 2026.

---

## D17 — tsunami.gov's bulletin category is never mapped to CAP severity

**Decision.** `adapters/tsunami.py` reads the per-entry `Category:` field
tsunami.gov's NTWC/PTWC Atom feeds embed in each bulletin's `<summary>` and maps
it, through an explicit table (`CATEGORY_TO_EVENT`), to the full verbatim level
name — `"Tsunami Information Statement"`, `"Tsunami Watch"`, `"Tsunami
Advisory"`, `"Tsunami Warning"` — and puts that in `event`. `severity` stays
`None`; the raw category goes in `source_severity` only. Every entry that
reaches `NormalisedAlert` at all carries a verified `Category:` (an entry
without one is quarantined first, D3), so `severity` lands in
`unmapped_fields`, never `unavailable_fields` — the source stated a level,
and alertmux is the one declining to translate it (see D18).

**Why.** tsunami.gov's own hierarchy is Information Statement < Watch <
Advisory < Warning. An Information Statement is the routine, most common
case — it typically means an earthquake occurred and no destructive tsunami
is expected — not an escalation. There is no verified CAP mapping from this
source's category to Minor/Moderate/Severe/Extreme; inventing one, or worse,
letting an Information Statement read as anything resembling a warning,
would be the single most dangerous thing this project could do. This is the
same discipline as `gdacs:alertlevel` (impact score, not severity) and
USGS's PAGER `alert` level — see the module docstrings and D1 — applied to a
source where the stakes of getting it wrong are the highest in the
codebase.

**Verified vs. documented-only.** Both live feeds (17-18 Aug 2026) carried
only `"Information"` bulletins — this project has never observed a live
Watch, Advisory or Warning. Those three are in `CATEGORY_TO_EVENT` on the
strength of tsunami.gov's own published terminology (present in every
"Definition:" text these bulletins carry), the same documentation-only basis
GDACS's `EVENT_TYPES` keeps `"VO"` (volcano) on. A category outside the
table is quarantined (D3), never defaulted or passed through raw.

**This will look like an oversight — the opposite of D1's SWIC codes.**
Here the "helpful fix" someone might propose is mapping `"Warning"` straight
to CAP `Extreme`/`Severe`, since it looks obviously safe by comparison to
GDACS's alertlevel. It is guarded by
`test_information_statement_never_produces_a_non_null_severity` in
`tests/test_tsunami.py` and the live smoke test
`test_tsunami_live_never_states_a_severity_for_an_information_statement`.

**Cost of being wrong.** Getting this backwards in either direction is
dangerous: relaying an Information Statement (the common case) as a
`severity` value would manufacture false alarms at scale; a mis-mapped
`Warning` read as low-severity would suppress the one case where this
source matters most. A real, verified CAP file for a Watch/Advisory/Warning
bulletin — none available at build time — would be the evidence needed to
add a severity mapping, and even then only for the levels it actually
covers.

---

## D18 — `unmapped_fields` is distinct from `unavailable_fields`

**Decision.** `NormalisedAlert` carries two lists, and a field that comes
back `None` is named in exactly one, never both:

- `unavailable_fields` — the source supplied nothing for this field.
- `unmapped_fields` — the source supplied a value, but alertmux declined to
  translate it, either because the mapping is unverified (an SWIC `s`/`u`/`c`
  code outside the D1 tables) or because it is deliberately never attempted
  (GDACS's `alertlevel`, a tsunami bulletin category, the USGS PAGER
  `alert` level — see D17 and the `gdacs.py`/`usgs.py` module docstrings).
  The raw value is preserved in `source_severity`/`source_urgency`/
  `source_certainty` either way. A `STRUCTURAL_GAPS` entry — a concept the
  feed never carries on any record, e.g. NWS's `instruction` or EONET's
  `severity` — always stays in `unavailable_fields` regardless of the
  particular record; structural absence is not a refusal to map.

**Why.** Before this, `unavailable_fields` conflated two different claims.
Verified against live code prior to this decision:

```
s=0  (SWIC supplied a code, refused to map it):
     severity=None  source_severity='0'   'severity' in unavailable_fields: True

no s (SWIC supplied nothing at all):
     severity=None  source_severity=None  'severity' in unavailable_fields: True
```

Both cases produced the identical signal. A consumer could only tell them
apart by inspecting every `source_*` field by hand, which defeats the
purpose of a single list a caller can trust without reading the schema. In a
hazard system the two claims are materially different: "the authority did
not say" versus "the authority said something specific and alertmux is
refusing to guess at its meaning." GDACS is the sharpest example — nearly
every live item carries `gdacs:alertlevel` (Green/Orange/Red), so `severity`
was landing in `unavailable_fields` on almost every GDACS alert, reading as
"GDACS never states this" when GDACS was in fact stating something on
nearly every record and being overruled on principle.

**Cost of being wrong.** The conflation itself was low-severity — no field
value was ever fabricated, so principle 2 was never actually violated — but
it made the API harder to trust than it should have been: a consumer
auditing `unavailable_fields` for gaps worth chasing down (e.g. "should I
fetch CAP detail for this alert?") could not distinguish "nothing to fetch,
the source never had it" from "something was stated, and there might be a
CAP file with a more specific answer." Splitting the two makes that
consumer's decision mechanical instead of requiring them to read every
adapter's source to know which case they are in.

**What would justify changing it back.** Nothing found so far. The two
questions ("did the source say anything" vs "did we choose to translate
what it said") are genuinely different and both worth asking; collapsing
them back into one list would restore the original ambiguity for no
benefit.
## D19 — USGS relays the M4.5+ past-day feed, not `all_hour`

**Decision.** `UsgsAdapter` defaults to
`summary/4.5_day.geojson` (magnitude ≥ 4.5, past 24 hours), replacing
`summary/all_hour.geojson`. The feed is a constructor argument
(`UsgsAdapter(feed="...")`), so an operator can choose another USGS
summary without touching code.

**Why not `all_hour`.** Measured 18 Aug 2026 (issue #20): `all_hour`
carries every quake in the past hour regardless of magnitude — live
sample M1.3, M0.9, M1.0 — while GDACS reports significant events
globally over several days (M5.6 Indonesia, M5.7 Mexico, M6.1
Vanuatu...). Two consequences, both measured: (1) the alerts are mostly
not hazards — noise against 3,138 alerts from other sources; (2)
cross-source dedupe barely triggers, because GDACS and USGS coincide
only when a large quake happens to fall inside the hour. Dedupe's design
assumed a *standing* overlap; with a 1-hour window there was not one.

**Why `4.5_day` rather than `significant_week`.** Both were named in the
issue as candidates. `4.5_day` wins on three grounds:

- **Every GDACS earthquake is inside the M4.5+ set.** GDACS's own 18 Aug
  sample was all M5.5-M6.1, so every GDACS quake now has a USGS
  counterpart by construction — the standing overlap dedupe was built
  for, restored. That is what makes cross-source dedupe *meaningful*
  again: instead of a near-empty intersection of two unrelated lists,
  dedupe now pairs genuine "same event, two sources" records.
- **A 24-hour rolling window fits "current alerts".** alertmux relays
  what is happening now; `significant_week` would hold an M6.1 for seven
  days after it happened. A quake older than a day dropping out of the
  feed is a feature.
- **M4.5+ is where quakes start being felt and damaging.** The relay's
  job is hazards, and a felt, potentially damaging shake is a hazard.

**Upstream threshold, not client-side filter.** Choosing a threshold feed
means the filtering happens on USGS's side — cheaper, and there is
nothing to forget to configure. Client-side filtering of `all_day` was
rejected: it re-implements (and can drift from) an upstream guarantee.
The constructor argument covers the one thing upstream thresholds
cannot: an operator who wants a different bar (e.g. `significant_week`
for impact-level events only, `1.0_day` for regional coverage).

**The cost, stated plainly.** Everything below M4.5 is *deliberately*
dropped. A user who sees no M2 quakes in the relay is seeing this
threshold at work, not a bug: the M2s existed, they are simply below the
relay's bar. That is the price of the GDACS overlap — a lower bar would
bring the M0.9-M2 noise back and dissolve the dedupe story. The trade is
reversible: `UsgsAdapter(feed="1.0_day")` (or `all_hour`) restores the
small quakes for anyone who wants them, so the cost is a visible,
operator-side choice, never a silent one.

**What would justify changing the default.** Measured consumer demand
for impact-only quakes, or evidence that the 24-hour window misses
events a relay must carry.

**How a user finds out it is deliberate, not broken.** README and
`docs/DATA-SOURCES.md` both state the feed and the threshold up front,
and this entry records the reasoning and the tradeoff; a missing M2
quake therefore has a discoverable explanation instead of reading as a
defect.

---

## Things we got wrong, kept here on purpose

Recorded because the failure *modes* recur, and because a project that only documents
its successes teaches nothing.

- **Fixtures test what you imagined; live data tests what is true.** Two critical
  defects (D2's unstable id, and a GeoServer error at HTTP 200 parsing as "no hazards
  anywhere") survived 51 passing tests and six independent code reviews. Both were
  invisible to fixtures because fixtures are small, static, and well-formed. The live
  smoke suite exists for this reason.
- **A single 404 is not proof a directory does not exist.** `/v2/cap-alerts/rss.xml`
  404s, so the whole `/v2/cap-alerts/` path was written off — it turned out to hold
  every CAP file. Probe with a filename you know is real.
- **`rlink` looked like a description and was a filename.** The fixture had it empty
  throughout, so every test passed while ~26% of live alerts would have surfaced a
  `.xml` path as alert text. When a field's meaning is inferred from its name, check
  it against live data.
- **A test can assert something true regardless of the implementation.** See D2. Ask
  of any test: what change would make this fail? If the answer is "none", it is
  documentation wearing a test's clothes.
