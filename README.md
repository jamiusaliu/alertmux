# alertmux

One API for natural-hazard alerts from multiple official sources, normalised
to a single schema with provenance intact.

**alertmux relays alerts published by official authorities. It never
originates a warning, and it is not a substitute for official warnings from
the issuing authority.**

## Sources

| Source | Coverage |
|---|---|
| WMO SWIC | 59 national alerting authorities, warnings currently in force |
| USGS | Global earthquakes ≥ M 4.5, past day (feed configurable — see [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md)) |
| NOAA / NWS | United States, active alerts |
| GDACS | Global disaster alerts (earthquakes, floods, cyclones, drought, wildfire) |
| NASA EONET | Satellite-observed events (wildfires, severe storms) — observations, not warnings |
| tsunami.gov (NTWC + PTWC) | US tsunami bulletins (Information Statement, Watch, Advisory, Warning) |

## Install

```bash
pip install -e ".[dev]"
```

## Run

```bash
uvicorn alertmux.api:app --reload
```

- `GET /alerts` — all current alerts. Optional `?authority=ng-nimet`.
- `GET /alerts/{alert_id}/detail` — one alert's raw CAP file, merged in. SWIC's
  list view (what `/alerts` serves) structurally omits `headline`,
  `description`, `instruction`, `onset` and `expires` — `expires` matters
  most, since without it a live warning can't be told from a lapsed one.
  This route fetches the authority's original CAP 1.2 file for that one
  alert and merges it into the record; where the CAP file states a named
  severity/urgency/certainty, that value wins over the list view's
  integer-code mapping (the raw code stays in `source_severity` etc.
  regardless). **Not** part of `/alerts`'s default response — with ~2,200
  alerts in force, fetching one CAP file per alert on every list poll would
  be ~2,200 requests to WMO per fetch, so this is opt-in per alert, and
  cached by `capurl` forever (a CAP file's path is content-addressed, so it
  can never change once published — see `docs/DECISIONS.md` D16). Returns
  **404** for an unknown `alert_id` and **502** if the CAP file can't be
  fetched or parsed — never a silently empty or partial record.
- `GET /health` — per-source health. Returns **HTTP 503** whenever the result
  is partial, so a standard monitor sees the degradation.
- `GET /sources` — discovery: what alertmux covers and what it misses. Per-source
  identity, structural gaps, authorities actually seen, and `hazard_coverage` /
  `uncovered_hazards` (a heuristic keyword classification — see `sources.py`).
- `GET /authorities` — the WMO Register of Alerting Authorities: 300 official
  alerting authorities across 199 countries, a directory of who is *allowed*
  to issue CAP alerts, not a source of alerts itself. Optional
  `?country=` (alpha-2 or alpha-3). `countries_covered` / `countries_uncovered`
  join at country level only — WMO's own authority abbreviations disagree
  with the ones alertmux's sources use, so authority-level matching is
  refused; see `registry.py` and `docs/DECISIONS.md`. Cached with a long TTL;
  `register_cache_age_seconds` always reports how stale the served copy is.

Any response where a source failed, returned fewer alerts than it holds, or
quarantined one or more unparseable records sets `partial: true`. Incomplete
results are always labelled — `sources[].invalid_count` and `invalid_samples`
say how many records were dropped and why.

With `?authority=` applied, `sources[].alert_count` describes the **whole
fetch** and will not equal `len(alerts)`. Source health is about the fetch,
not the filter.

If `?authority=` matches nothing, the response also carries
`available_authorities` — the authorities present in this fetch — so a typo
is distinguishable from a genuinely quiet day. That list is only ever built
from alerts the current fetch actually returned, so it also carries
`available_authorities_partial`: `true` means the underlying fetch was
partial (a source was down, truncated, or dropped bad records), in which
case a missing authority may simply have a source that failed right now,
not one that does not exist. Check that flag before treating a short list
as proof an authority is invalid.

Results are cached for 60 seconds, so polling `/health` does not repeatedly
pull ~774KB from WMO.

## Use from Claude

alertmux ships an MCP server so a Claude user can query live alerts from
inside a conversation, without going through the HTTP API. `mcp` is an
optional dependency — installing it pulls in a second HTTP client
(`httpx2`), `cryptography`, `opentelemetry-api` and a few other packages
that the core library does not otherwise need, so it stays opt-in (see
`docs/DECISIONS.md`).

```bash
pip install "alertmux[mcp]"
```

This installs the `alertmux-mcp` console script, which speaks MCP over
stdio:

```bash
alertmux-mcp
```

Add it to a client's MCP config (e.g. Claude Desktop's
`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "alertmux": {
      "command": "alertmux-mcp"
    }
  }
}
```

Four tools are exposed — `list_alerts`, `list_sources`,
`get_hazard_coverage`, and `find_duplicates` — all reading through the same
cached fetch path the HTTP API uses, so an MCP client never hammers WMO
independently of anyone else querying alertmux. Every tool response carries
the relay disclaimer and the `partial`/`truncated` flags from the query
layer, so a model relaying the answer can say plainly when data is missing
or incomplete rather than reporting it as a clean "no alerts."

## An example alert

A real warning from the Nigerian Meteorological Agency, as alertmux returns it:

```json
{
  "id": "wmo-swic:ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml",
  "event": "THUNDERSTORMS",
  "headline": null,
  "description": null,
  "instruction": null,
  "area_description": "Some states in Nigeria will be affected.",
  "severity": "Severe",
  "urgency": "Expected",
  "certainty": "Observed",
  "source_severity": "3",
  "source_urgency": "3",
  "source_certainty": "4",
  "sent": "2026-08-17T06:50:16Z",
  "onset": null,
  "expires": null,
  "geometry": null,
  "provenance": {
    "authority": "ng-nimet",
    "source_id": "wmo-swic",
    "source_url": "https://severeweather.wmo.int/g/wfs",
    "retrieved_at": "2026-08-17T23:39:03Z",
    "raw_reference": "ng-nimet-en/2026/08/17/14/50/16-e28162fa92b40b8a59c979ba00b562e9.xml"
  },
  "unavailable_fields": [
    "description",
    "expires",
    "geometry",
    "headline",
    "instruction",
    "onset"
  ],
  "unmapped_fields": []
}
```

Read it as: the authority stated a severity code of `3`, which is confirmed to
mean CAP `Severe`, so both are reported. It supplied no headline, description,
instruction, onset, expires or polygon — the SWIC list view carries none of
them; they live only in the raw CAP file at
`https://severeweather.wmo.int/v2/cap-alerts/<raw_reference>`. Every one of
those absences is named in `unavailable_fields`, which is exhaustive: if a
field is `null`, its name is in `unavailable_fields` or `unmapped_fields`
(empty here — see below). `GET /alerts/{alert_id}/detail` fetches that CAP
file and fills every one of those gaps it can.

`unavailable_fields` and `unmapped_fields` answer two different questions,
and every `null` field is in exactly one, never both:

- `unavailable_fields` — the source supplied nothing for this field.
- `unmapped_fields` — the source supplied a value, but alertmux declined to
  translate it, either because the mapping is unverified (an SWIC `s`/`u`/`c`
  code outside the confirmed tables) or because it is deliberately never
  attempted (GDACS's `alertlevel`, a tsunami bulletin category, the USGS
  PAGER level — none of which are CAP severity). The raw value is still in
  `source_severity`/`source_urgency`/`source_certainty` either way.

A GDACS alert with `source_severity: "Green"` and `severity: null` has
`"severity"` in `unmapped_fields`, not `unavailable_fields` — the authority
said something specific, and alertmux is refusing to translate it, which is a
different claim from "the authority said nothing."

The `id` is derived from `raw_reference`, not from the GeoServer feature id —
GeoServer's synthetic fids embed the *request* timestamp and change on every
fetch, so they cannot be used as a deduplication key.

## Design rules

- **Nothing is inferred.** A field a source does not supply is `null`, and its
  name appears in `unavailable_fields` — or, if the source did supply a value
  that alertmux declined to translate, `unmapped_fields`. Never both.
- **Only verified severity codes are translated.** SWIC's `s`/`u`/`c` are
  integers. The mapping was confirmed against raw CAP files, so `severity`,
  `urgency` and `certainty` carry proper CAP names — but any code that was
  never observed stays `null` rather than being guessed, and lands in
  `unmapped_fields` (the code was there, it just wasn't trusted). The raw
  value is always preserved in `source_severity` / `source_urgency` /
  `source_certainty`, even when a mapping exists.
- **A malformed record is quarantined, never defaulted.** A feature missing
  an identity, an event type, or a timezone on its timestamp is skipped and
  counted rather than guessed at or allowed to discard every other record in
  the same response — one bad SWIC feature out of 2,200 no longer costs the
  other 2,199. The source stays `ok: true` (it answered), `partial` is forced
  `true` (the answer is incomplete), and `sources[].invalid_count` /
  `invalid_samples` say how many records were dropped and why.
- **A malformed envelope still fails loudly.** A 200 response that is not
  valid GeoJSON/RSS at all — not one bad record but a broken feed — still
  turns into `ok=false` for that source. Quarantine only applies once the
  envelope is confirmed genuine.
- **Adapters never raise outward.** A failing source returns a status, so one
  broken feed cannot take down a response.

## Running the tests

```bash
pytest          # the whole unit suite; no network, fixtures only
pytest -m live  # hits the real WMO SWIC, USGS, NOAA/NWS, GDACS and NASA EONET endpoints
```

Live tests are excluded from the default run and from CI, so neither depends
on third-party uptime. A new adapter's live test belongs in
`tests/test_live_smoke.py`, under the `live` marker.

## Adding a source

1. Create `src/alertmux/adapters/yoursource.py` with a class exposing
   `source_id: str` and `fetch() -> FetchResult`. Copy `usgs.py` — it is the
   simplest example. Note that `FetchResult` requires `retrieved_at` and
   `latency_ms`; neither has a default.
2. Record a real payload to `tests/fixtures/yoursource_<feed>.json`, matching
   the existing naming (`swic_effective.json`, `usgs_4.5_day.json`).
3. Write `tests/test_yoursource.py` against that fixture. No network in unit
   tests — use `respx` to mock `fetch()`.
4. Register it in `src/alertmux/adapters/__init__.py`. This is **three**
   edits, not one:

   ```python
   from alertmux.adapters.yoursource import YourSourceAdapter          # 1. import

   __all__ = ["SwicAdapter", "UsgsAdapter", "YourSourceAdapter",       # 2. __all__
              "default_adapters"]


   def default_adapters():
       return [SwicAdapter(), UsgsAdapter(), YourSourceAdapter()]      # 3. register
   ```

5. Add a live smoke test to `tests/test_live_smoke.py` under the `live` marker.

### The two rules your adapter must follow

1. **Never map an unverified code.** Translate a source's severity, urgency or
   certainty to a CAP name only after confirming that meaning against that
   source's own raw CAP output. Anything unconfirmed leaves the named field
   `None` and keeps the raw value in `source_*`. A guessed severity is either
   a missed warning or a false alarm — do not complete a partial table.
2. **Record every `null` optional field in exactly one of `unavailable_fields`
   / `unmapped_fields`.** `unavailable_fields` means the source supplied
   nothing; `unmapped_fields` means it supplied a value your adapter declined
   to translate (an unverified code, or a value deliberately never mapped,
   like GDACS's `alertlevel`). Both lists are exhaustive by contract and must
   never overlap. Derive them from the values you actually built and from
   whether the source's raw field was present, as `swic.py` and `gdacs.py`
   do, rather than appending entries by hand.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and commit style.

## Documentation

Deeper reference, kept in `docs/` and updated as the project grows:

| Document | What it covers |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the modules fit together, and what each defensive check is guarding against |
| [`docs/DATA-SOURCES.md`](docs/DATA-SOURCES.md) | Every endpoint in detail — including WMO SWIC's undocumented WFS API, the 59 authority codes, and the confirmed CAP severity mapping |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Why things are the way they are, what each choice costs if wrong, and what evidence would justify changing it |

If you are about to "fix" something that looks obviously wrong — particularly the
incomplete severity tables — read `docs/DECISIONS.md` first. It is probably
deliberate, and the entry will tell you what evidence would change our mind.

## Licence

MIT.
