# Connection Reliability

This context describes recurring German train journeys and the outcomes recorded for them.

## Language

**Monitored connection**:
A recurring journey defined by a fixed, ordered sequence of named train legs and boarding and alighting stops. It is checked on selected calendar days.

**Train leg**:
One specified train service within a monitored connection, from its boarding stop to its alighting stop.
_Avoid_: Connection, route

**Transfer**:
The change between consecutive train legs at a station. Its feasibility depends on the time between the first train's arrival and the next train's departure.
_Avoid_: Connection

**Transfer margin**:
The minimum time required for a transfer to be considered feasible. It can be set per transfer and defaults to five minutes.

**Worked connection**:
A monitored connection for which no train leg was reported cancelled and every transfer met its transfer margin according to reported train times. This is a reported outcome, not proof that a passenger travelled.

**Failed connection**:
A monitored connection for which at least one train leg was explicitly cancelled or at least one transfer did not meet its transfer margin.

**Eligible day**:
A date on which a monitored connection is due to be checked under its calendar rule. For a weekday rule, nationwide German public holidays are excluded.

**Unknown outcome**:
An eligible day's connection for which the available train reports do not establish whether it worked or failed. A missing timetable entry alone does not establish cancellation. Unknown outcomes are counted separately from worked and failed connections.

**Sample**:
An eligible day's connection with enough evidence to classify it as worked or failed. A success rate is the number of worked connections divided by the number of samples.

**Final arrival delay**:
The difference between the scheduled and reported arrival time at a monitored connection's destination, expressed in minutes.
