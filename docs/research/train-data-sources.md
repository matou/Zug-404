# Train data sources for the reliability collector

Researched 2026-10-01. Scope: one fixed, recurring Augsburg Hbf → München Hbf → Berlin Hbf journey, tentatively RE 89 around 08:08 and ICE 1100 around 09:19. The collector needs each day's planned stops, reported changes or cancellations, and enough timely observations to assess the transfer after travel. **The example's trains and times have not been verified against a real service day.**

## Source comparison

| Source | Fit for this collector | What still needs validation |
| --- | --- | --- |
| [DB Timetables API product](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables) | Candidate to test first for planned stops and reported changes at each station. The station and change endpoint behavior still needs confirmation from official documentation and live responses. | The product page returned HTTP 403 here. Confirm the current endpoint contract, station identifier lookup, authentication, personal-use terms, quotas, price, change retention, and whether the reported time at a completed stop is an actual or a continuing estimate. No live request or sample match was possible. |
| DB [RIS:Journeys](https://developers.deutschebahn.com/db-api-marketplace/apis/product/ris-journeys) and [RIS:Boards](https://developers.deutschebahn.com/db-api-marketplace/apis/product/ris-boards) product pages | Possible alternative if journey-level data makes exact train matching or completed-leg status more reliable. | Both pages returned HTTP 403 here. Product availability, endpoint names, payload semantics, coverage, historical access, authentication, quotas, cost, and permitted storage are **unverified**. Do not design an adapter around assumed RIS fields. |
| DELFI / regional GTFS Realtime feeds (locate a first-party publisher before choosing one) | GTFS Realtime can represent trip updates, stop arrival/departure predictions, cancellations, and skipped stops. It would also require a matching planned timetable and stable trip IDs. | No official feed for both sample legs was verified. National coverage, ICE inclusion, update frequency, post-journey retention, access conditions, and GTFS static-to-realtime mapping are unknown. A feed with only regional service would not cover the whole connection. |

The [GTFS Realtime protocol definition](https://github.com/google/transit/blob/master/gtfs-realtime/proto/gtfs-realtime.proto) confirms that `TripUpdate` may contain predicted or past stop events, arrival and departure `StopTimeEvent`s, feed/update timestamps, and schedule relationships for cancelled trips and skipped stops. Those are **format capabilities**, not evidence that any DELFI feed supplies them consistently for this journey. In particular, a missing trip update alone is not a cancellation.

## Recommendation

Treat **DB Timetables as the provisional first source to validate**, not a committed provider. It appears closest to the plan's station-by-station, named-train lookup, while the RIS and GTFS options need basic product or coverage verification first. The official DB documentation and API were inaccessible from this environment, so this is a testing order rather than a conclusion about quality or cost. Keep the data-source boundary small enough to replace after a live feasibility check.

Before implementing collection, use the official product documentation and a real service date to establish:

1. Whether RE 89 from Augsburg Hbf and ICE 1100 from München Hbf resolve unambiguously, connect at München Hbf, and serve the configured destination. Record station IDs, trip IDs, planned times, and train numbers from the responses.
2. Whether a planned stop can be joined to a change record across both stations and repeated polls. Capture examples of an unchanged stop, a changed arrival/departure, a cancellation, and a completed stop. Determine which timestamps are predictions versus actual reports.
3. How long changed and completed-stop data remain available after each leg. Poll shortly after arrival and throughout the retry window; preserve each response locally. Set cron cadence and the unknown-outcome deadline from observed retention, not an assumed daily API archive.
4. Current registration requirements, authentication, request quotas, costs, storage/publication terms, and whether a private, locally generated statistics page is permitted. Inspect the terms attached to the chosen product before obtaining credentials or retaining data indefinitely.

Until those checks pass, the planned rule “no reported change after a successful timely poll means scheduled time with a *no reported change* label” remains an inference, not proof of actual on-time travel. Missing feed access or an unmatched train remains **unknown**. A leg counts as cancelled only with an explicit cancellation report; a reported transfer below its minimum also makes the connection **failed**.

## Source access and evidence limits

- Official DB product pages: [Timetables](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables), [RIS:Journeys](https://developers.deutschebahn.com/db-api-marketplace/apis/product/ris-journeys), and [RIS:Boards](https://developers.deutschebahn.com/db-api-marketplace/apis/product/ris-boards). All returned HTTP 403 during this research. Their current terms and technical details could not be read; the RIS URLs themselves should be confirmed through the marketplace catalog.
- [GTFS Realtime protocol source](https://github.com/google/transit/blob/master/gtfs-realtime/proto/gtfs-realtime.proto), maintained in the Google Transit repository, was accessible. It establishes protocol fields only, not a German feed's availability or quality.
- [DELFI](https://www.delfi.de/) and the [German public transport open-data portal](https://www.opendata-oepnv.de/) returned HTTP 403 here. No first-party DELFI feed contract was available to verify.
