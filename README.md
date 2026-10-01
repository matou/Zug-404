# Zug-404

Zug-404 is a planned collector for the reliability of recurring German train connections. It will use reported train times and cancellations to classify each eligible journey as worked, failed, or unknown, then show success rates, sample counts, arrival delays, and a date-by-date history.

The first proposed connection is Augsburg Hbf → München Hbf → Berlin Hbf, using RE 89 and ICE 1100 on weekdays outside nationwide German public holidays. Its exact timetable has not yet been verified.

## Status

This repository contains planning and data-source research; there is no runnable collector yet. Collection is intended to start at deployment, with no historical backfill.

## Documents

- [Implementation plan](PLAN.md) — configuration, collection, outcome rules, and verification.
- [Domain terms](CONTEXT.md) — definitions used for connection outcomes and statistics.
- [Data-source research](docs/research/train-data-sources.md) — candidate APIs and remaining feasibility checks.
