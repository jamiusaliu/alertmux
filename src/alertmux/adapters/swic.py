"""WMO Severe Weather Information Centre adapter.

One adapter, 59 national alerting authorities. Uses the
effective_warning_view layer, which returns only warnings currently in
force — expired alerts never enter the pipeline, so they can never be
re-notified.

Two rules that must not be relaxed:

1. Always paginate. Unfiltered queries exceed 24MB and time out at 60s.
2. Map only verified s/u/c codes. The tables below were confirmed
   against raw CAP for seven distinct combinations with no conflicts.
   Any code outside them stays untranslated - the raw value is kept
   and the named field left None. Guessing an unseen code means
   either a missed warning or a false alarm.

The raw CAP file for any alert resolves at
https://severeweather.wmo.int/v2/cap-alerts/<capurl>. It carries
expires, onset, instruction and polygon, which this list view omits.
Fetching one file per alert is too expensive for a list endpoint, so
v0.1 does not.

Pagination and truncation: GeoServer caps a response at `maxFeatures`.
When `numberMatched > numberReturned` the server had more warnings in
force than it returned, so this adapter sets `FetchResult.truncated` and
that flag propagates to `AlertsResponse.partial`. v0.1 LABELS truncation
rather than silently dropping alerts; true `startIndex` pagination —
looping until `numberReturned` is exhausted — is the v0.2 remedy.

Identity: the GeoServer synthetic feature `id` embeds a REQUEST
timestamp, so it changes on every fetch and cannot be a stable alert id.
The alert id is derived from `capurl`, which is the identity of the CAP
file itself and is stable across fetches.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult
from alertmux.schema import NormalisedAlert, Provenance

USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"

_AUTHORITY_RE = re.compile(r"^([a-z]{2}-[a-z0-9]+)")

# Confirmed against raw CAP 1.2, 17 Aug 2026, 7 samples, no conflicts.
# Deliberately partial: codes never observed are absent, not guessed.
SEVERITY = {1: "Minor", 2: "Moderate", 3: "Severe", 4: "Extreme"}
URGENCY = {2: "Future", 3: "Expected", 4: "Immediate"}
CERTAINTY = {2: "Possible", 3: "Likely", 4: "Observed"}


def _authority_from_capurl(capurl: str | None) -> str:
    """capurl looks like 'ng-nimet-en/2026/08/17/...xml'.

    Authority is mandatory provenance. An unparseable capurl raises
    rather than inventing a placeholder authority.
    """
    if not capurl:
        raise ValueError("SWIC feature has no capurl; cannot derive authority")
    match = _AUTHORITY_RE.match(capurl)
    if match is None:
        raise ValueError(
            f"SWIC capurl {capurl!r} does not carry a parseable authority prefix"
        )
    return match.group(1)


def _iso_utc(value: str | None, field: str = "timestamp") -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an offset.

    A naive value would be interpreted as server-local time by
    astimezone(), silently shifting a hazard timestamp by the deploying
    machine's UTC offset. Refuse instead of guessing.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"SWIC {field} {value!r} has no UTC offset; refusing to assume one"
        )
    return parsed.astimezone(timezone.utc)


def _require_feature_collection(payload: dict) -> None:
    """A 200 that is not a FeatureCollection is an error, not a quiet day.

    GeoServer answers a bad cql_filter, an unknown typeName or backend
    trouble with an OWS exception report at HTTP 200 and no `features`
    key. Treating that as zero alerts would report "no hazards anywhere"
    as a healthy result.
    """
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
            or "features" not in payload:
        raise ValueError(
            "SWIC response is not a GeoJSON FeatureCollection: "
            f"{repr(payload)[:400]}"
        )


class SwicAdapter:
    """Fetches and normalises WMO SWIC effective warnings."""

    source_id = "wmo-swic"
    URL = "https://severeweather.wmo.int/g/wfs"
    TYPE_NAME = "local_postgis:effective_warning_view"

    def __init__(
        self,
        client: httpx.Client | None = None,
        mem: str | None = None,
        max_features: int = 3000,
        timeout: float = 60.0,
    ):
        self._client = client
        self._mem = mem
        self._max_features = max_features
        self._timeout = timeout

    def build_params(self, mem: str | None, max_features: int) -> dict:
        params = {
            "request": "GetFeature",
            "version": "1.1.0",
            "typeName": self.TYPE_NAME,
            "outputFormat": "json",
            "maxFeatures": max_features,
        }
        if mem:
            params["cql_filter"] = f"mem='{mem}'"
        return params

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        _require_feature_collection(payload)

        alerts: list[NormalisedAlert] = []

        for feature in payload["features"]:
            props = feature.get("properties")
            if not props:
                raise ValueError(
                    f"SWIC feature {feature.get('id')!r} has no properties"
                )

            event = props.get("event")
            if not event:
                raise ValueError(
                    f"SWIC feature {feature.get('id')!r} has no event"
                )

            capurl = props.get("capurl")
            if not capurl:
                raise ValueError(
                    f"SWIC feature {feature.get('id')!r} has no capurl; "
                    "the synthetic GeoServer fid is not a stable identity"
                )

            def _code(key: str) -> str | None:
                value = props.get(key)
                return str(value) if value is not None else None

            def _named(key: str, table: dict[int, str]) -> str | None:
                """Map a code only if it was verified. Never guess.

                SWIC emits s/u/c as JSON numbers, but some authorities send
                them as digit strings ("3" instead of 3). A string key
                against the int table misses silently and leaves the named
                field null, so coerce digit strings to int before lookup.
                Genuinely unmappable values (None, non-digit strings, codes
                outside the verified table) still fall through to None and
                land in unavailable_fields exactly as before.
                """
                raw = props.get(key)
                if isinstance(raw, str) and raw.isascii() and raw.isdigit():
                    raw = int(raw)
                return table.get(raw)

            # The WFS list view carries no headline and no description;
            # both live only in the CAP file. `rlink` is a path to a
            # RELATED CAP file and is not a description of this alert.
            fields = {
                "headline": None,
                "description": None,
                "area_description": props.get("areadesc"),
                "severity": _named("s", SEVERITY),
                "urgency": _named("u", URGENCY),
                "certainty": _named("c", CERTAINTY),
                "source_severity": _code("s"),
                "source_urgency": _code("u"),
                "source_certainty": _code("c"),
                "sent": _iso_utc(props.get("sent"), "sent"),
                # onset/expires live in the CAP file, not this list view.
                "onset": None,
                "expires": None,
                "geometry": feature.get("geometry"),
            }

            # Source-shape entries: fields this feed structurally never
            # supplies. Unioned with every optional field that came back
            # None, so the list is exhaustive by construction rather than
            # by remembering to append.
            structural = ("onset", "expires", "headline", "description")
            unavailable = sorted(
                set(structural) | {k for k, v in fields.items() if v is None}
            )

            alerts.append(
                NormalisedAlert(
                    id=f"{self.source_id}:{capurl}",
                    event=event,
                    provenance=Provenance(
                        authority=_authority_from_capurl(capurl),
                        source_id=self.source_id,
                        source_url=self.URL,
                        retrieved_at=retrieved_at,
                        raw_reference=capurl,
                    ),
                    unavailable_fields=unavailable,
                    **fields,
                )
            )

        return alerts

    def fetch(self) -> FetchResult:
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(
            timeout=self._timeout, headers={"User-Agent": USER_AGENT}
        )

        try:
            response = client.get(
                self.URL, params=self.build_params(self._mem, self._max_features)
            )
            response.raise_for_status()
            payload = response.json()
            alerts = self.parse(payload, retrieved_at)
        except Exception as exc:  # noqa: BLE001 - adapters never raise outward
            return FetchResult(
                source_id=self.source_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                retrieved_at=retrieved_at,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            if self._client is None:
                client.close()

        matched = payload.get("numberMatched")
        returned = payload.get("numberReturned")
        truncated = (
            isinstance(matched, int)
            and isinstance(returned, int)
            and matched > returned
        )

        return FetchResult(
            source_id=self.source_id,
            ok=True,
            alerts=alerts,
            retrieved_at=retrieved_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            truncated=truncated,
            matched=matched if isinstance(matched, int) else None,
            returned=returned if isinstance(returned, int) else None,
        )
