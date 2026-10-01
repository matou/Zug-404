# Train connection statistics plan

## Goal

Record the reported outcome of specified recurring train connections in Germany and show how often each connection worked, the number of usable samples, final arrival delay, and a complete date-by-date table. Start collecting when the tool is deployed; do not promise historical backfill.

## First connection

- Journey: Augsburg Hbf to Berlin Hbf, changing at München Hbf.
- Leg 1: RE89 from Augsburg Hbf to München Hbf, departing around 08:08.
- Leg 2: ICE 1100 from München Hbf to Berlin Hbf, departing around 09:19.
- Calendar: Monday through Friday, excluding public holidays observed throughout Germany.
- Transfer margin: five minutes by default, with an optional override for any transfer.

The exact planned stops and times are to be discovered from timetable data. Validate that RE89 reaches München Hbf before ICE 1100 departs and that ICE 1100 serves Berlin Hbf; the sample timetable could not be independently verified from this environment. If the named trains or stops cannot be matched unambiguously, flag the configuration for review rather than silently substituting another service.

## Connection configuration

Put one TOML file per monitored connection in `connections/`. Python 3.11 or newer can read TOML with its standard library. Each file has a stable ID, display name, calendar rule, and ordered train legs. `scheduled_departure` is the nominal time used to match a train; the timetable supplies that day's exact planned time. For example, `connections/augsburg-berlin.toml` could contain:

```toml
id = "augsburg-berlin"
name = "Augsburg Hbf → Berlin Hbf"
weekdays = ["mon", "tue", "wed", "thu", "fri"]
exclude_holidays = "de_nationwide"

[[legs]]
train = "RE 89"
from = "Augsburg Hbf"
to = "München Hbf"
scheduled_departure = "08:08"

[[legs]]
train = "ICE 1100"
from = "München Hbf"
to = "Berlin Hbf"
scheduled_departure = "09:19"

# Optional: if [[transfers]] is omitted, the minimum is 5 minutes.
# [[transfers]]
# station = "München Hbf"
# min_minutes = 7
```

Each adjacent pair of legs defines a transfer: the first leg's `to` station must match the next leg's `from` station. The minimum is five minutes by default. An optional `[[transfers]]` entry overrides the minimum at its named station; it must match one of the transfers implied by the legs. The collector scans the folder on each run and checks which valid connections are due. It processes due connections independently. A malformed or ambiguous config is reported on the index page and in logs; it does not stop the other connections. The ID, not the filename, identifies its history, so renaming a file does not split the statistics. Removing a file stops new checks for that connection but keeps its existing local history.

## Outcome rules

An eligible day is **worked** when every named train leg is present, none is reported cancelled, and every transfer meets its minimum margin using the data source's reported times. It is **failed** when a leg is explicitly cancelled or a transfer is reported below its margin. It is **unknown** when neither outcome can be established, including when a train is absent from the timetable without an explicit cancellation. For a source that only reports deviations from its planned timetable, a successful, timely check with no change entry for a matched planned stop uses its scheduled time and is labelled **no reported change**; it is not proof of actual on-time operation. A missing or late check does not qualify for that inference.

Show eligible days, worked days, failed days, unknown days, and sample count separately. A sample is a day with a worked or failed outcome; the success rate is `worked / samples`. Label feasibility and arrival delay as based on reported times, which may be estimates and do not prove a passenger boarded.

## Collection

Use a small Python program on an always-on Linux machine. Read all connection files from `connections/` and use the timetable to resolve each exact scheduled service for every eligible day.

Run collection shortly after each leg's scheduled end and repeat while a result is incomplete or the train is still running, subject to a bounded retry window. Save observations as they arrive so that short-lived change data is not lost. If evidence remains insufficient after the window, record an unknown outcome and the reason. Collection must be idempotent so scheduler retries cannot create duplicate daily rows. A cron job can run the collector periodically on Linux; macOS is for manual testing.

Keep the local history indefinitely in SQLite. Store the reported and planned times, cancellations, observation timestamps, source status, computed transfer margins, and outcome for each eligible date. Re-render static HTML after updates. The first version can have an index and one local page per monitored connection. Its collapsed full table has one row per eligible date, with all train details and missing-data reasons available in that row.

## Data-source research before implementation

Research how best to obtain the planned timetable and timely post-journey reports before choosing or building a data adapter. Use the repo's `research` skill to compare official sources against the actual needs of this collector: matching the named trains and stops, recording cancellations and changed times, retention after travel, authentication, cost, quotas, and permitted use. The [data-source research note](docs/research/train-data-sources.md) records initial findings and unresolved limits; official product terms and a live journey still need validation. The collection schedule and outcome rules may need adjustment based on what the chosen source really provides.

One candidate to investigate is [DB Timetables](https://developers.deutschebahn.com/db-api-marketplace/apis/product/timetables), using station plans and reported changes. Its developer registration appears to require an application and API credentials; current pricing, quotas, and retention were not verifiable here. Key-free DB interfaces have [reported reliability and permission concerns](https://github.com/public-transport/db-vendo-client), and some boards can omit cancelled trains. No source has been selected yet.

DB Timetables' change feed appears to report deviations rather than a confirmed actual timestamp for every stop. A missing change entry means no change was reported; it does not prove the train arrived on time. Changed times can be estimates or actuals. The outcome rule above states how to use this limited evidence if this source is selected.

Run a short feasibility check for the sample journey before committing to an API adapter:

1. Check current terms, cost, and quotas for personal use.
2. Resolve the named trains, exact stops, planned times, and destination station for a real date.
3. Observe planned, changed, cancelled, and on-time cases; validate when an absent change entry can safely be labelled no reported change.
4. Check how soon after each leg the change feed becomes incomplete, then set the polling and retry window accordingly.

If the official API requires registration or has a cost the user does not want, revisit the source choice before implementing the collector. Do not infer a cancellation from an absent timetable entry.

## Verification

Use recorded API responses to test train matching, explicit cancellations, transfer margins at four, five, and six minutes, incomplete data, holiday exclusion, and overnight delays. Confirm that repeated collection updates the same date rather than inflating sample count. Open the static pages locally on macOS and confirm the summary agrees with the date table.
