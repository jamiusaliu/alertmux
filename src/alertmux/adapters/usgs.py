"""USGS earthquake feed adapter.

USGS publishes observed earthquakes, not forecast warnings, so it has no
expiry -- recorded as unavailable, since the feed never supplies it at
all. `severity` is different: most features carry no PAGER `alert`
level (too small to score), but when they do, it is kept as a
source-native value only — it is not CAP severity and must not be
mapped to one. `severity` therefore lands in `unavailable_fields` when
`alert` is absent and `unmapped_fields` when it is present but declined,
exactly like `gdacs:alertlevel` (see `gdacs.py`).

The feed also carries non-earthquake events (`quarry blast`, `explosion`,
`ice quake`, `sonic boom`, `mining explosion`), so `type` is never
defaulted to "earthquake" — a missing type is a malformed feature.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from alertmux.adapters.base import FetchResult, RecordQuarantine
from alertmux.schema import NormalisedAlert, Provenance

AUTHORITY = "us-usgs"
USER_AGENT = "alertmux/0.1 (+https://github.com/jamiusaliu/alertmux)"


def _epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _require_feature_collection(payload: dict) -> None:
    """A 200 that is not a FeatureCollection is an error, not a quiet hour."""
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection" \
            or "features" not in payload:
        raise ValueError(
            "USGS response is not a GeoJSON FeatureCollection: "
            f"{repr(payload)[:400]}"
        )


class UsgsAdapter:
    """Fetches and normalises the USGS magnitude-4.5+ past-day summary.

    The default feed is the upstream magnitude-thresholded
    ``summary/4.5_day.geojson`` rather than ``all_hour``: a relay carrying
    every quake in the past hour is mostly M0.9-M2 noise, and it barely
    overlaps GDACS (see DECISIONS.md D19). ``feed`` is a constructor
    argument so an operator can choose another USGS summary (e.g.
    ``significant_week`` for impact-level events only, or ``1.0_day`` for
    a broader net) without touching code.
    """

    source_id = "usgs"
    DEFAULT_FEED = "4.5_day"
    URL = (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
        f"summary/{DEFAULT_FEED}.geojson"
    )

    # USGS reports observed earthquakes, not forecast warnings: no
    # expiry, no CAP urgency/certainty, no description field at all.
    # A class attribute so /sources can report it without a fetch.
    STRUCTURAL_GAPS: tuple[str, ...] = (
        "urgency",
        "certainty",
        "onset",
        "expires",
        "description",
        "instruction",
    )

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        feed: str = DEFAULT_FEED,
    ):
        self._client = client
        self._timeout = timeout
        # Instance-level URL so provenance and /sources report the feed
        # actually configured, not the class default.
        self.URL = (
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
            f"summary/{feed}.geojson"
        )

    def parse(self, payload: dict, retrieved_at: datetime) -> list[NormalisedAlert]:
        """A malformed envelope (not a FeatureCollection) is a hard
        failure. A single malformed feature is quarantined instead --
        skipped, counted, sampled on the returned list (see
        `RecordQuarantine`), never allowed to discard the whole
        response (DECISIONS.md D3).
        """
        _require_feature_collection(payload)

        alerts = RecordQuarantine()

        for feature in payload["features"]:
            try:
                props = feature.get("properties")
                if not props:
                    raise ValueError(
                        f"USGS feature {feature.get('id')!r} has no properties"
                    )

                feature_id = feature.get("id")
                if not feature_id:
                    raise ValueError("USGS feature has no id")

                # The feed carries quarry blasts and explosions too. Calling
                # one of those an earthquake would be an invented fact.
                event = props.get("type")
                if not event:
                    raise ValueError(
                        f"USGS feature {feature_id!r} has no type"
                    )

                # PAGER alert level, not CAP severity. Kept source-native only.
                pager = props.get("alert")

                fields = {
                    "headline": props.get("title"),
                    # USGS has no description field. None means "the source
                    # has none" here exactly as it does for SWIC.
                    "description": None,
                    "area_description": props.get("place"),
                    # USGS states no CAP severity/urgency/certainty at all.
                    "severity": None,
                    "urgency": None,
                    "certainty": None,
                    "source_severity": str(pager) if pager else None,
                    "source_urgency": None,
                    "source_certainty": None,
                    "sent": _epoch_ms(props.get("time")),
                    # Observed events: these concepts do not apply.
                    "onset": None,
                    "expires": None,
                    "geometry": feature.get("geometry"),
                }

                # The PAGER level is only sometimes present -- most feature
                # records carry no `alert` at all (the majority of events
                # are too small for PAGER to score). When it IS present,
                # severity is deliberately not derived from it (not CAP
                # severity); that is unmapped, not unavailable.
                unmapped = ["severity"] if fields["source_severity"] is not None else []

                unavailable = sorted(
                    (set(self.STRUCTURAL_GAPS) | {k for k, v in fields.items() if v is None})
                    - set(unmapped)
                )

                alerts.append(
                    NormalisedAlert(
                        id=f"{self.source_id}:{feature_id}",
                        event=event,
                        provenance=Provenance(
                            authority=AUTHORITY,
                            source_id=self.source_id,
                            source_url=self.URL,
                            retrieved_at=retrieved_at,
                            raw_reference=props.get("url"),
                        ),
                        unavailable_fields=unavailable,
                        unmapped_fields=unmapped,
                        **fields,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad record is quarantined, not fatal
                alerts.quarantine(exc)

        return alerts

    def fetch(self) -> FetchResult:
        retrieved_at = datetime.now(tz=timezone.utc)
        started = time.monotonic()
        client = self._client or httpx.Client(
            timeout=self._timeout, headers={"User-Agent": USER_AGENT}
        )

        try:
            response = client.get(self.URL)
            response.raise_for_status()
            alerts = self.parse(response.json(), retrieved_at)
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

        return FetchResult(
            source_id=self.source_id,
            ok=True,
            alerts=alerts,
            retrieved_at=retrieved_at,
            latency_ms=int((time.monotonic() - started) * 1000),
            invalid_count=alerts.invalid_count,
            invalid_samples=alerts.invalid_samples,
        )
