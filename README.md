# Zug-404

Zug-404 collects reported outcomes for recurring German train connections. It uses DB Timetables station plans and changes to classify each eligible journey as worked, failed, or unknown, then writes local static HTML with success rates, sample counts, reported arrival delays, and a date-by-date history.

The first proposed connection is Augsburg Hbf → München Hbf → Berlin Hbf, using RE 89 and ICE 1100 on weekdays outside nationwide German public holidays. **Its exact timetable has not yet been verified with authenticated API responses.** `check` reports an error if either named service cannot be matched unambiguously.

## Requirements and local setup

- Python 3.11 or newer, with time zone data for `Europe/Berlin` (Linux normally includes it).
- A DB Timetables subscription and its `DB-Client-ID` and `DB-Api-Key` values.

The collector reads credentials only from `DB_CLIENT_ID` and `DB_API_KEY` in the **user-run process environment**. Keep them outside this repository and do not send or paste them into an agent session. They are never written into the SQLite database or report. Configure them on your own machine, then run:

```sh
python3 zug404.py check --date YYYY-MM-DD
python3 zug404.py collect
python3 zug404.py render
python3 -m http.server 8000 --directory site
```

Use a real eligible date for `check`. It prints the resolved stop IDs and planned times, or an error if the sample train or station cannot be matched. `collect` starts 30 minutes before the first departure, checks the current and previous service date, and stops after an 18-hour window. Run it every minute on an always-on Linux machine, from the repository root. For example, use a cron entry that changes to the repository directory and invokes `python3 zug404.py collect`. Keep the scheduler's credentials in its private environment. Pages go to `site/`; observations, source plan slices, and history go to `data/zug404.sqlite3`. Both paths are ignored by Git.

The full change feed loses entries as trips leave stations. Every successful poll is saved, including an empty change entry for a matched stop. An unchanged scheduled time is used only when a successful poll occurred from two minutes before through ten minutes after that station event, and is labelled “no reported change.” Changed times may be estimates. Missing evidence produces `unknown`, and no historical backfill is attempted. Repeated runs update the same date.

**Before unattended deployment or publication**, clarify DB's terms for API polling, indefinite local retention, and publishing aggregate statistics, as described in the [data-source research](docs/research/train-data-sources.md). The sample service and the feed's behavior around completed arrivals also need a live check on your machine. No authenticated API call has been run in this workspace.

Run the local tests with `python3 -m unittest discover -s tests`.

## Documents

- [Implementation plan](PLAN.md) — configuration, collection, outcome rules, and verification.
- [Domain terms](CONTEXT.md) — definitions used for connection outcomes and statistics.
- [Data-source research](docs/research/train-data-sources.md) — candidate APIs and remaining feasibility checks.
