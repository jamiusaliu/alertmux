# Data sources

Everything known about the feeds alertmux reads, including things not documented
anywhere else. All observations are dated; **re-verify before relying on them.**
Public feeds change without notice.

> **Keeping this current:** when you add an adapter, add a section with the exact
> endpoint, an observed response, the fields it supplies, and — critically — the
> fields it *does not*. When a feed changes shape, record the date and what changed
> rather than overwriting history. Someone debugging a two-year-old alert needs to
> know what the feed looked like then.

---

## WMO SWIC — Severe Weather Information Centre

**The primary source: one endpoint, 59 national alerting authorities.**

SWIC publishes no public API documentation. The endpoint below was recovered by
reading the site's own JavaScript bundles (`geoserver1.js` builds its URLs from a
`gsdmn` host variable) and confirmed from the live site's network traffic.

### List endpoint

```
https://severeweather.wmo.int/g/wfs
  ?request=GetFeature
  &version=1.1.0
  &typeName=local_postgis:effective_warning_view
  &outputFormat=json
  &maxFeatures=3000
  [&cql_filter=mem='<authority code>']
```

Observed 17 Aug 2026: **2,145 warnings in force globally**, 774,979 bytes for the
full set.

Notes that cost time to learn:

- The path is **`/g/wfs`**, not `/geoserver/wfs`. Every conventional GeoServer path
  404s.
- **`GetCapabilities` 404s.** Only `GetFeature` is exposed, so the layer list cannot
  be discovered the normal way.
- **`effective_warning_view` returns only warnings currently in force.** This is the
  layer to use — expired alerts never enter the pipeline and so can never be
  re-notified.
- **Always send `maxFeatures`.** An unfiltered query on the raw layer exceeded 24MB
  and timed out at 60s.
- **`startIndex` pagination works, but only paired with `sortBy`.** GeoServer
  WFS 1.1.0 honours `startIndex` on this endpoint; `SwicAdapter.fetch()`
  (18 Aug 2026, issue #3) loops on it, requesting a further page whenever
  the previous one came back filled to `maxFeatures` or `numberMatched` says
  more remain, up to a `max_pages` ceiling. Before this, v0.1 only labelled
  truncation at a single `maxFeatures=3000` cap — safe while live counts sat
  around 2,100–2,300, but not once a severe day pushes past it. See
  DECISIONS.md D4.
  **`startIndex` alone is not enough — verified live, 18 Aug 2026: sending
  `startIndex` without `sortBy` gets HTTP 200 back with the literal body
  `Err\nErr\nErr\nErr\nErr\nErr\n`**, not JSON and not a proper WFS
  `ExceptionReport`. WFS 1.1.0 does not mandate a feature order, and this
  server enforces that a sort key must accompany paging rather than falling
  back to some default order. `sortBy=capurl` fixes it — confirmed live,
  two consecutive pages returned disjoint, alphabetically contiguous slices
  with zero overlap — and costs nothing since `capurl` is already the
  unique, stable identity field (D2). Always send both together.
- A second layer, `local_postgis:postgis_geojsons` on `/f/wfs`, carries geometry and
  is filtered by `row_type` (`POLYGON` held 4,732 features; `POINT` 96; `LINE` and
  `CIRCLE` were empty). `effective_warning_view` returns `geometry: null`.
- **A bad `cql_filter` or `typeName` returns HTTP 200** with an OWS exception report
  and no `features` key. This is why the adapter validates the envelope.

### Response fields

```
capurl, sent, event, s, u, c, mem, areadesc, rlink
```

- `capurl` — path to the authority's original CAP file. **Content-addressed and
  stable**, which is why alert ids derive from it.
- `rlink` — **a path to a RELATED CAP file, not a description.** Non-empty in 554 of
  2,133 alerts observed. Mapping it to a description surfaces a filename as alert
  text; do not use it.
- The synthetic `feature["id"]` (e.g.
  `effective_warning_view.fid--7463f54d_1a0121513f5_-1f9d`) **embeds a request
  timestamp** — the middle segment decodes as epoch millis of the query. It changes
  on every fetch. Confirmed: the same Nigeria alert fetched twice seconds apart
  returned ids ending `_2d5b` then `_2d5c` with an identical `capurl`. **Never use it
  as an identity.**
- The list view supplies **no** `headline`, `description`, `onset`, `expires`,
  `instruction` or `polygon`. Those live only in the CAP file.

### Detail endpoint — the raw CAP file

```
https://severeweather.wmo.int/v2/cap-alerts/<capurl>
```

Returns full **CAP 1.2**, digitally signed (`ds:Signature`), with namespaced tags
(`cap:severity`, not `severity`). Example (NiMet, 17 Aug 2026, 4,598 bytes):

```xml
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <cap:identifier>urn:oid:2.49.0.1.566.0.2026.8.17.14.50.16</cap:identifier>
  <cap:sender>cfo@nimet.gov.ng</cap:sender>
  <cap:status>Actual</cap:status>
  <cap:severity>Severe</cap:severity>
  <cap:urgency>Expected</cap:urgency>
  <cap:certainty>Observed</cap:certainty>
  <cap:onset>2026-08-17T19:16:00+01:00</cap:onset>
  <cap:expires>2026-08-18T06:00:00+01:00</cap:expires>
  <cap:headline>THUNDERSTORMS OVER PARTS OF NIGERIA</cap:headline>
```

Not fetched by `/alerts` — one request per alert is too expensive for a list
endpoint. It is the natural source for `expires`, which the notifier will
need. Fetched on request only (issue #4, 18 Aug 2026):
`SwicAdapter.fetch_detail(capurl)` and `GET /alerts/{alert_id:path}/detail`,
cached by `capurl` forever — the path is content-addressed (a hash of the
file's own content), so the same `capurl` can never resolve to different
bytes once published. See DECISIONS.md D16.

Two real fetches (18 Aug 2026) confirm the shape:

```xml
<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
<cap:identifier>IN-1787070151015041_43</cap:identifier>
<cap:sender>IMD-Chennai</cap:sender>
<cap:info>
<cap:language>en-IN</cap:language>
<cap:urgency>Expected</cap:urgency>
<cap:severity>Moderate</cap:severity>
<cap:certainty>Likely</cap:certainty>
<cap:onset>2026-08-18T21:52:31+05:30</cap:onset>
<cap:expires>2026-08-19T01:00:00+05:30</cap:expires>
<cap:headline>Light to Moderate Rain with Thunderstorm...</cap:headline>
<cap:description/>
<cap:instruction>Please follow SDMA guidelines.</cap:instruction>
</cap:info>
</cap:alert>
```

(India NDMA, `tests/fixtures/swic_cap_detail.xml`.) Notes:

- **Some authorities use the default namespace instead of a bound `cap:`
  prefix** (observed: China CMA files declare
  `xmlns="urn:oasis:names:tc:emergency:cap:1.2"` with unprefixed tags, e.g.
  `<severity>` not `<cap:severity>`). Both resolve to the same namespace URI
  and parse identically through `ElementTree`'s own namespace map — the
  adapter's `cap:` prefix is ours, not the source document's.
- **Fields live inside `<info>`, not directly on `<alert>`.** The excerpt
  earlier in this section (the original recovery notes) showed severity/
  urgency/etc. as if they were direct children of `<alert>` — that was a
  simplification for the write-up, not the real structure. `parse_cap_detail`
  reads `cap:info/cap:severity` etc.
- **A CAP file can carry more than one `<info>` block** — observed on a
  China CMA alert with parallel English and Chinese blocks. The adapter
  prefers the one whose `<language>` starts with `en`, falling back to the
  first block otherwise.
- **`<cap:description/>` (empty) is real.** Parsed as `None`, not `""` — an
  empty element is not a stated description.
- **No inline `<cap:polygon>` observed** in ~40 live samples (18 Aug 2026);
  one authority (`in-ndma`) instead links a polygon via a `<cap:parameter>`
  named `"Polygon URL"` pointing at a separate NDMA endpoint, which this
  project does not follow. `parse_cap_detail` supports an inline
  `<cap:area><cap:polygon>` if one is ever observed, converting CAP's
  `"lat,lon lat,lon ..."` text to GeoJSON, but this path is untested against
  real data — treat it as provisional until a live sample exercises it.

> Finding this cost several rounds: `/v2/cap-alerts/rss.xml` 404s, and that single
> 404 was taken as evidence the whole directory did not exist. **Probe a directory
> with a file you know exists, never with a guessed filename.**

### Severity / urgency / certainty codes — CONFIRMED

`s`, `u` and `c` are integers. The mapping was established by fetching the raw CAP
for seven distinct combinations and comparing. **No conflicts.**

```
s=1 u=2 c=2  ->  Minor     Future     Possible
s=2 u=2 c=4  ->  Moderate  Future     Observed
s=2 u=3 c=2  ->  Moderate  Expected   Possible
s=3 u=3 c=2  ->  Severe    Expected   Possible
s=3 u=3 c=4  ->  Severe    Expected   Observed
s=4 u=4 c=2  ->  Extreme   Immediate  Possible
s=4 u=4 c=3  ->  Extreme   Immediate  Likely
```

| code | severity | urgency | certainty |
|---|---|---|---|
| 0 | *never observed* | *never observed* | *never observed* |
| 1 | **Minor** | *never observed* | *never observed* |
| 2 | **Moderate** | **Future** | **Possible** |
| 3 | **Severe** | **Expected** | **Likely** |
| 4 | **Extreme** | **Immediate** | **Observed** |

It is the CAP ordinal scale ascending by intensity.

**Only bold cells are mapped in code.** By the pattern, urgency `1` "should" be
`Past` and certainty `1` "should" be `Unlikely` — but neither was ever observed, and
pattern-matching is not evidence. See `DECISIONS.md` for why this is not an
oversight, and what evidence would justify adding them.

### Authority codes (`mem`) — 59 observed, 17 Aug 2026

Derived by fetching all in-force warnings and mapping `mem` to the `capurl` prefix.
**Derive this at runtime; never hardcode it.** WMO adds members.

| mem | authority | mem | authority | mem | authority |
|---|---|---|---|---|---|
| 001 | cn-cma | 062 | fr-meteofrance | 103 | bg-meteo |
| 003 | pt-ipma | 064 | gw-inm | 105 | lt-lhms |
| 005 | ba-fhmzbih | 066 | in-ndma | 106 | by-belhydromet |
| 006 | at-zamg | 067 | ie-met | 107 | ru-roshydromet |
| 008 | no-met | 070 | kz-kazhydromet | 108 | md-shs |
| 009 | pl-imgw | **075** | **ng-nimet** | 122 | dz-onm |
| 013 | il-met | 079 | sa-ncm | 137 | ec-inamhi |
| 015 | si-meteo | 083 | es-aemet | 139 | sb-sims |
| 016 | de-dwd | 085 | sd-sma | 169 | vu-vmgd |
| 017 | hu-met | 087 | ch-meteoswiss | 171 | cr-imn |
| 019 | hr-meteo | 088 | td-anam | 172 | cz-chmi |
| 021 | ph-pagasa | 089 | th-tmd | 176 | it-meteoam |
| 026 | jm-jms | 092 | ua-meteo | 177 | kg-meteo |
| 028 | cl-meteo | 093 | us-noaa | 179 | mx-smn |
| 037 | nl-rnmi | 094 | uy-inumet | 181 | cw-meteo |
| 038 | bz-nms | 095 | kr-kma | 183 | ro-meteoromania |
| 039 | ee-emhi | 096 | se-smhi | 185 | au-bom |
| 043 | id-inatews | 101 | rs-hidmet | 193 | me-meteo |
| 046 | nz-nms | 053 | be-irm | 055 | cm-meteo |
| 056 | ca-msc | 061 | fi-fmi | | |

---

## Source overlap — measured, not assumed

Verified 18 Aug 2026. The v0.1 design claimed SWIC "replaced" the NWS, GDACS and
EONET adapters because one endpoint covers 59 authorities. **That was wrong**, and
the numbers say so plainly.

### SWIC vs NWS — same domain, SWIC is the lossy copy

| | NWS direct | SWIC `mem='093'` (us-noaa) |
|---|---|---|
| Alerts in force | **292** | 204 (**69%**) |
| Fields per alert | **32** | 9 |

Missing from SWIC: 84 Small Craft Advisories, plus High Surf Advisory entirely.
NWS additionally supplies `description`, `instruction`, `headline`, `expires`,
`onset`, `effective`, `sender`, `category`, `response`, `status`, `messageType`,
and — decisively — **`severity`/`urgency`/`certainty` as named CAP values**, so no
integer-code mapping is needed at all.

`expires` matters most: it is the field a notifier needs to tell a live warning from
a lapsed one, and SWIC's list view does not carry it.

**Conclusion: use NWS directly for US coverage.** SWIC's `us-noaa` slice is strictly
worse on both count and depth.

### SWIC does not cover whole hazard families

Across all 2,235 SWIC alerts in force:

```
   694  wind/storm        206  wildfire/fire
   376  heat              115  flood
   269  rain                1  volcano
     2  snow/ice
     0  earthquake     <-- zero
     0  drought        <-- zero
     0  tsunami        <-- zero
```

SWIC is a **meteorological warning** system. GDACS carries what it cannot:
365 events — 315 wildfire, 19 flood, **16 earthquake, 12 drought**, 3 tropical
cyclone — each with an alert level and impact estimate
(*"Green earthquake, Magnitude 5.6M, Depth 10km, Indonesia, 3 thousand in MMI V"*).
EONET carries satellite-**observed** events: 195 wildfires, 5 severe storms.

The two groups answer different questions and are not interchangeable:

- **SWIC + NWS** — what authorities are **warning** about (forecast)
- **GDACS + EONET + USGS** — what is **happening** (observed)

A coverage metric counting only authorities is therefore misleading: 59/300
authorities can still mean zero drought and zero earthquake coverage. **Track
coverage by hazard family as well as by authority.**

## NOAA / NWS — United States

```
https://api.weather.gov/alerts/active
```

No key. GeoJSON, CAP-derived, 32 properties per feature.

- **`?limit=N` returns HTTP 400.** The parameter is not supported the way it looks;
  use the unparameterised endpoint.
- **`status` must be checked.** The live feed carries test traffic — a persistent
  `KEEPALIVE` record with `status: "Test"`, `event: "Test Message"`. Observed 1 of
  295. **Only `status == "Actual"` may be relayed as a warning.** Relaying a test
  message as a real hazard alert would violate the project's central promise. SWIC
  does not have this problem; it filters upstream (0 test events observed).
- `messageType` is `Alert` (189) or `Update` (106). `Cancel` exists in the CAP spec
  and must not surface as an active alert if it appears.
- **Only 48 of 295 features carry geometry**; the rest are zone-referenced via
  `affectedZones` / `geocode`. A null geometry here means "referenced by zone", not
  "unknown" — do not conflate the two.
- `severity` values observed: `Minor` 171, `Severe` 66, `Moderate` 49, `Unknown` 9.
  `Unknown` is a real CAP value, not a missing field.

## GDACS — global disaster alerts

```
https://www.gdacs.org/xml/rss.xml
```

RSS with a `gdacs:` namespace. 365 items observed. Event types via
`<gdacs:eventtype>`: `WF` wildfire 315, `FL` flood 19, `EQ` earthquake 16,
`DR` drought 12, `TC` tropical cyclone 3.

Namespaced tags include `gdacs:alertlevel` (Green/Orange/Red), `gdacs:alertscore`,
`gdacs:country`, `gdacs:bbox`, `gdacs:eventtype`, `gdacs:cap`, `gdacs:severity`.

**`gdacs:alertlevel` is an impact score, not CAP severity.** Green/Orange/Red
describe expected humanitarian impact. Do not map it onto CAP's
Minor/Moderate/Severe/Extreme — that would assert a severity GDACS never stated.
Keep it in `source_severity`.

The JSON API at `/gdacsapi/api/events/geteventlist/MAP` returned 400 for every
parameter name tried. Use the RSS.

**Implemented in `adapters/gdacs.py`, 18 Aug 2026 (issue #13).** The first
RSS/XML adapter in the codebase — parsed with the standard library's
`xml.etree.ElementTree`, no new dependency. Notes gathered while building it:

- **Identity: `gdacs:eventid` + `gdacs:episodeid`, not `<guid>`.** The RSS
  `<guid>` on this feed is only `{eventtype}{eventid}` (e.g. `EQ1559738`) —
  stable, but coarser than needed, since the same disaster accumulates
  multiple episodes as it evolves (the earthquake fixture item is
  `eventid=1559738`, `episodeid=1726926`). Verified stable by fetching the
  live feed twice, 18 Aug 2026: the same 369 `(eventid, episodeid)` pairs
  came back identical on both fetches.
- **`gdacs:eventtype` needs an explicit table, same as SWIC's severity
  codes.** Observed in the live feed that day: `WF` 319, `FL` 19, `EQ` 16,
  `DR` 12, `TC` 3 — 369 total. `VO` (volcano) is in the mapping table on the
  strength of GDACS's own documentation, though it was not present in that
  day's sample.
- **No `urgency`, `certainty`, or `expires` at all** — not even as unmapped
  source-native values. All three are structurally unavailable on every
  alert.
- **Timestamps are RFC-822** (`pubDate`, `gdacs:fromdate`), not ISO-8601 —
  parsed with `email.utils.parsedate_to_datetime`, not `fromisoformat`.
- **Geometry:** `geo:Point` is used when present (most precise); `gdacs:bbox`
  (`lonmin lonmax latmin latmax`) is mapped to a rectangular GeoJSON Polygon
  when there is no point — a faithful reading, not an approximation, since a
  bbox unambiguously denotes a rectangle.
- A fixture trimmed to 5 items (`tests/fixtures/gdacs_rss.xml`, one each of
  EQ/DR/WF/TC/FL) was recorded from the live feed the same day; real field
  names and values throughout, no invented data.
- **`gdacs:country` (`area_description`) is country-level only — never a
  locality.** It is just the country name (`"Angola"`, `"Brazil"`), not a
  region, province, or town. This is what makes `event` + `area_description`
  a dangerously coarse identity key for GDACS specifically: 98 separate
  Angola wildfires, each with its own distinct `gdacs:eventid`, all carry
  `area_description="Angola"` and would collapse onto one
  `wildfire|angola` key with nothing to tell them apart if grouped naively.
  `dedupe.py` guards against this by requiring every member of a candidate
  group to come from a different `provenance.source_id` — see
  DECISIONS.md D13's 18 Aug 2026 addendum. Anything else built on top of
  `area_description` (filtering, display grouping, a future notifier) should
  expect the same coarseness from this source and not assume country-level
  text means "the whole country is affected."

## tsunami.gov — NTWC + PTWC tsunami bulletins

```
https://www.tsunami.gov/events/xml/PAAQAtom.xml   National Tsunami Warning Center (NTWC, Palmer AK)
https://www.tsunami.gov/events/xml/PHEBAtom.xml   Pacific Tsunami Warning Center (PTWC, Honolulu HI)
```

No key. Both verified live 17-18 Aug 2026. **Measured: this was the one hazard
family with zero coverage across all five other sources** — SWIC is a
meteorological warning system and carries no tsunamis at all (see "SWIC does not
cover whole hazard families" above); NWS *can* emit `Tsunami Warning`/`Advisory`/
`Watch` but only when one is active in US waters, and none was at measurement time.
`/sources` reported `tsunami` in `uncovered_hazards` before this adapter existed.

**Atom, not RSS.** `<entry>`, not `<item>`, under the default namespace
`http://www.w3.org/2005/Atom`. `geo:lat` / `geo:long`
(`http://www.w3.org/2003/01/geo/wgs84_pos#`) on each entry. Implemented in
`adapters/tsunami.py` (issue #15) with `xml.etree.ElementTree`, no new dependency.

Observed shape, both feeds, 17-18 Aug 2026 (each carried exactly one `<entry>`):

```xml
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#">
  <title>Tsunami Information Statement Number 1</title>
  <updated>2026-08-17T20:39:52Z</updated>
  <entry>
    <title>100 miles SW of Kodiak City, Alaska</title>
    <updated>2026-08-17T20:39:52Z</updated>
    <geo:lat>57.200</geo:lat>
    <geo:long>-154.800</geo:long>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <strong>Category:</strong> Information<br/>
      ...
      <strong>Definition: </strong>An information statement indicates that an
      earthquake has occurred, but does not pose a tsunami threat, or that a
      tsunami warning, advisory, or watch has been issued for another section
      of the ocean. <a href="...">View bulletin</a>
    </div></summary>
    <id>urn:uuid:3f6aa6dd-f007-48f2-8ff7-806d50e1da95</id>
    <link rel="related" title="CapXML document" href="..." type="application/cap+xml" />
    <link rel="alternate" title="Bulletin" href="..." type="application/xml" />
  </entry>
</feed>
```

### The statement hierarchy — this is the whole point

```
Information Statement  <  Watch  <  Advisory  <  Warning
```

**An Information Statement is NOT a warning.** It is the routine, most common
case — it typically means an earthquake occurred and no destructive tsunami is
expected. Both live feeds carried only Information Statements at the time this
adapter was built. See DECISIONS.md D17 for the full reasoning: the bulletin
category is never mapped to CAP `severity` (stays `None`, always in
`unavailable_fields`); it is kept verbatim, mapped through an explicit table, in
`event` (`"Tsunami Information Statement"`, not a generic `"Tsunami"`) and raw in
`source_severity`.

### Where the level actually lives

**Not in the entry `<title>`** — that is the affected region ("100 miles SW of
Kodiak City, Alaska"), same role GDACS's `area_description` plays.

**Not reliably in the feed-level `<title>`** either, even though it states the
level verbatim ("Tsunami Information Statement Number 1") — that title describes
the feed's current/latest bulletin, not necessarily every `<entry>` it might ever
contain. Used here only for `headline` (feed-scoped, applied per entry — correct
for the single-entry feeds observed; would need revisiting if a feed is ever
observed carrying more than one entry with differing levels).

**The level comes from the entry's own `<summary>`**, specifically the
`Category: <word>` line embedded in its XHTML body (`Information` observed;
`Watch`/`Advisory`/`Warning` are tsunami.gov's own documented categories, unobserved
live). `adapters/tsunami.py`'s `_category()` extracts it with a regex over the
summary's concatenated text rather than parsing the presentational `<strong>`/`<br/>`
markup structurally. A category outside `CATEGORY_TO_EVENT` is quarantined (D3), not
defaulted.

### Identity and other notes

- **Identity is the entry's own `<id>`** (a `urn:uuid:...`), namespaced by centre:
  `tsunami-gov:{us-ntwc|us-ptwc}:{entry-id}`. Verified stable — two independent
  fetches of both feeds, seconds apart, 18 Aug 2026, returned byte-identical
  responses, entry `<id>` included.
- **Timestamps are ISO-8601 with an explicit `Z`** on every entry observed —
  parsed and required to carry an offset, same as every other adapter. There is no
  separate `onset`; the only timestamp is the bulletin's issue/update time, used as
  `sent`.
- **Structurally never supplied:** `urgency`, `certainty`, `expires`, `instruction`,
  and a distinct `onset` — none of these concepts appear on this feed at all, not
  even as unmapped source-native values.
- **`raw_reference` prefers the CapXML link** (`rel="related" title="CapXML
  document"`) over the plain-text bulletin link, mirroring GDACS's `gdacs:cap`
  fallback to `<link>`.
- Two authorities from one adapter, same pattern as SWIC covering 59: NTWC and PTWC
  are different offices with different coverage areas (NTWC: Alaska, Canada, US
  East/Gulf coasts; PTWC: Hawaii and the wider Pacific), so `provenance.authority`
  distinguishes `us-ntwc` from `us-ptwc` rather than collapsing both into one
  `tsunami-gov` authority. `fetch()` fetches and merges both; one centre's outage
  does not take the other's alerts down with it — recorded as a quarantined entry
  (`invalid_samples`) naming the failed centre, and only a total failure of both
  centres makes `ok=False`.
- Fixtures (`tests/fixtures/tsunami_ntwc_atom.xml`, `tests/fixtures/tsunami_ptwc_atom.xml`)
  are the real feed content captured 17-18 Aug 2026, byte-for-byte (only
  reformatted for readability, no content changed) — both were Information
  Statements; there was no live Watch/Advisory/Warning to capture. Tests exercising
  those three levels modify a copy of the real fixture's `Category:` field, the
  same technique `test_gdacs.py` uses for its unknown-eventtype case — never a
  fabricated alert.

## NASA EONET — satellite-observed events

```
https://eonet.gsfc.nasa.gov/api/v2.1/events?limit=<n>
```

Clean JSON, no key. Event fields: `id`, `title`, `description`, `link`,
`categories`, `sources`, `geometries`.

Observed categories: Wildfires 195, Severe Storms 5.

**These are observations, not warnings.** EONET has no severity, no urgency, no
certainty, no expiry — those concepts do not apply, and all must be recorded as
structurally unavailable rather than invented. `geometries` is a list of timestamped
points or polygons; an event may have many as it is tracked over time.

Largely overlaps GDACS on wildfires. Its value is independent satellite
corroboration of an event another source is only forecasting.

## USGS — earthquakes

```
https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson
```

Clean GeoJSON, no key, well documented upstream. **Default feed is the
magnitude-thresholded past-day summary (`4.5_day`), deliberately NOT
`all_hour`** — see DECISIONS.md D19. `all_hour` carries every quake in the
past hour regardless of magnitude (M0.9-M2 noise), and a 1-hour window barely
overlaps GDACS; `4.5_day` carries actual hazards and every GDACS earthquake
(M5.5+) falls inside it, so cross-source dedupe gets the standing overlap it
was built for. A user seeing no M2 quakes should know that is deliberate —
see DECISIONS.md D19.

The feed is a constructor argument, not a constant:

```python
UsgsAdapter()                      # default: summary/4.5_day.geojson
UsgsAdapter(feed="significant_week")  # impact-level events only
UsgsAdapter(feed="1.0_day")           # broader net, M1.0+ past day
```

Any USGS summary name works (`all_day`, `2.5_day`, `significant_week`, ...);
it is interpolated into the URL, so `provenance.source_url` and `GET /sources`
report the feed actually configured. Operator choice, not code change.

Three traps:

- **`time` and `updated` are epoch MILLISECONDS.** A seconds-based conversion puts
  events roughly 56,000 years in the future.
- **`alert` is a PAGER level** (`green`/`yellow`/`orange`/`red`), **not CAP
  severity.** It is kept in `source_severity` only; `severity` stays null. Mapping it
  to a CAP level would assert a severity USGS never stated. Observed in the
  `4.5_day` feed 18 Aug 2026: the M5.7 Mexico and M5.6 Indonesia quakes carry
  `alert=green`; the rest of the day's M4.5-5.3 events carry `alert=null`.
- `type` is not always `earthquake` — the feed also emits `quarry blast`,
  `explosion`, `ice quake`, `sonic boom`, `mining explosion`. Never default it.
- USGS reports *observed* events, so it supplies no `urgency`, `certainty`, `onset`
  or `expires`. All four are structurally unavailable.

---

## WMO Register of Alerting Authorities

```
https://alertingauthority.wmo.int/rss.xml     (also /atom.xml)
```

250KB of RSS, **300 items** — every official alerting authority worldwide, with the
CAP categories each covers. Nigeria's entry:

```
Nigeria: Nigerian Meteorological Agency
https://alertingauthority.wmo.int/authorities.php?recId=119
CAP categories: Geo Met Safety Security Health Env Transport CBRNE
```

It lists authorities, **not feed URLs**, so it is a directory rather than a source of
alerts. Its value is as an authoritative coverage map: 59 of 300 authorities are
reachable through SWIC today, and the gaps are a legitimate contribution backlog
nobody has to be persuaded matters.

Implemented in v0.3 as `registry.py` / `GET /authorities`.

> Practical note: this host returned 0 bytes to some clients and 250KB to others.
> If it comes back empty, change the user agent before concluding the feed is down.

### The join problem — country-level only, and why

Each item carries `title, link, description, guid, pubDate, author,
iso:countrycode, raa:authorityAbbrev, cap:area, cap:geocode, cap:polygon,
cap:value, cap:valueName, georss:box`. Two mismatches make a naive join from
this register to alertmux's own alert data impossible at the authority level.

**1. `iso:countrycode` is ISO 3166-1 alpha-3** (`NGA`, `USA`, `CYM`). Every
alertmux authority slug (`ng-nimet`, `us-noaa`) uses the alpha-2 prefix, and
the feed does not carry the alpha-3 → alpha-2 mapping anywhere. It cannot be
derived by truncating the alpha-3: `NGA` → `ng` and `CHE` → `ch` happen to
work that way, but `ZAF` → `za`, `DEU` → `de` and `GBR` → `gb` do not follow
any rule from their alpha-3 form. `registry.py`'s `ALPHA3_TO_ALPHA2` is an
explicit, embedded ISO 3166-1 table — not a heuristic.

Some items carry a `cap:geocode` with `valueName: iso-3166-1-alpha-2` and the
alpha-2 value directly (Nigeria's item does not; the USA's NOAA/NWS entry
does). Measured 18 Aug 2026: only 159 of 300 items carry this geocode at
all — including neither Nigeria's nor 140 others — so it cannot be relied on
as the mapping source; the embedded table covers all 300.

**2. `raa:authorityAbbrev` disagrees with SWIC's own abbreviation for the
same authority.** WMO records Nigeria's agency abbreviation as `nma`
(Nigerian Meteorological Agency); SWIC's `capurl` calls the identical
authority `nimet`. So alertmux's own slug `ng-nimet` never reconstructs from
`NGA` + `nma` — the two registries independently chose different short names
for the same agency, and there is no transform between them. Verified: the
one case where WMO's abbrev agrees with alertmux's own slug suffix is
`us-noaa` (WMO: `noaa`). That is not evidence the join generally works — it
is the coincidence that makes the failure easy to miss. Measured: the
register holds 300 authorities across 199 countries; alertmux's live alerts
carry 56 authorities across 52 alpha-2 country prefixes, and country-level
overlap between those two sets is real and useful; authority-level overlap
cannot be asserted without inventing a mapping nobody publishes.

**The consequence.** `GET /authorities` joins at country level only.
`countries_covered` / `countries_uncovered` compare the register's mapped
alpha-2 codes against the country prefix of authorities that actually
returned an alert this fetch. Each register entry also carries
`matched_authority`, populated only when `"{alpha2}-{abbrev}"` exactly
equals an authority slug seen this fetch (so `us-noaa` matches, `ng-nimet`
does not) — this is the deliberately rare, honest case, never generalised
into a claim the data cannot support. See docs/DECISIONS.md for the
decision record and its reasoning.

---

## Sources evaluated and rejected

| Source | Why not |
|---|---|
| **NiMet direct** (`nimet.gov.ng`) | No machine-readable feed. HTTP 307 to a JavaScript wall; `/api/warnings` 302s. Nigeria is reached through SWIC instead. |
| **MeteoAlarm** | Europe only. 404 for Nigeria. |
| **GDACS JSON API** | `/gdacsapi/api/events/geteventlist/MAP` returned 400 for every parameter name tried. The RSS feed at `gdacs.org/xml/rss.xml` works unambiguously. |
| **Ushahidi** | Dormant (2 commits in six months) and licensed `NOASSERTION` — no grant of rights to rely on. |
