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
   in `unavailable_fields`. Never defaulted, never guessed.
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
or an immediate warning read as past. An unmapped code costs a null and a name in
`unavailable_fields`, which is recoverable. The asymmetry is the whole argument.

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

## D3 — A malformed record raises rather than being skipped

**Decision.** A missing `capurl`, an unparseable authority prefix, a naive timestamp,
a missing USGS `type` or `id` — each raises `ValueError`, failing the whole fetch.

**Why.** Failing loudly beats quietly inventing. The alternative considered was
substituting a placeholder, which violates principle 2.

**Known cost, accepted for v0.1.** One bad record from one authority currently
discards the whole response — 1,900+ good warnings from 58 other services. That is
tolerable *only* because the failure is loud: `ok=false`, `partial=true`, HTTP 503.
The operator knows they are seeing nothing rather than believing an incomplete list
is complete.

**Superseding plan.** Per-feature quarantine: skip the malformed feature, never
invent a value for it, count it into `SourceStatus.invalid_count`, and force
`partial=true`. That satisfies both principle 2 and principle 4. Tracked for v0.2.

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

**Longer term.** v0.1 *labels* truncation; it does not paginate. Real `startIndex`
pagination is the proper remedy.

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
