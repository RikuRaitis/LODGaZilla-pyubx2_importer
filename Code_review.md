# Open review findings

## Claude J3 — Define the timezone of filename-derived NMEA dates

**Location:** [importer.py](importer.py), `Parser.parse()`, `parse_date_from_filename()`, and `TimeState.timestamp()`.

**Already fixed:** Time-only sentences advance the date when crossing UTC midnight.

**Still open:** A GGA-only recording initializes its date from the filename, then combines that date with NMEA time as UTC. If the filename reflects the logger's local date, recordings near local midnight can use the wrong UTC day. Midnight rollover cannot correct a wrong initial date.

**Proposed fix:** Prefer an explicit UTC date from the recording. For recordings without one, establish a logger timezone/date policy and convert the filename timestamp accordingly. Do not assume that the importing computer's timezone is the logger's timezone.

**Verification needed:** GGA-only logs starting near midnight in positive and negative UTC offsets, plus logs containing explicit NMEA dates. The logger timezone policy remains a product/data-format decision.
