"""pyubx2-backed GNSS parser."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from src.contracts.importer import DataParser, ProgressCallback, ProgressReporter, SeriesDefinition, add_gnss_distance_series, filter_gnss_time_outliers


GNSS_FORMAT_NAME = "gnss.pyubx2"
NMEA_START_RE = re.compile(br"\$(GP|GL|GA|GB|GN|GQ)[A-Z]{3},")
FILENAME_DATE_RE = re.compile(r"(20\d{2})[_-]?([01]\d)[_-]?([0-3]\d)")
MAX_UNTIMED_MESSAGE_DISTANCE = 20
DAY_ROLLOVER_SECONDS = 12 * 3600


@dataclass
class SeriesBuilder:
    """Incrementally collect one numeric time series."""

    definition: SeriesDefinition
    parser: DataParser
    time_values: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)

    def add(self, timestamp: float | None, value: Any) -> None:
        numeric_value = to_float(value)
        if timestamp is None or numeric_value is None or not math.isfinite(numeric_value):
            return
        self.time_values.append(timestamp)
        self.values.append(numeric_value)

    def to_dict(self) -> dict[str, Any]:
        _key, payload = self.parser.make_series_from_definition(
            self.definition,
            self.time_values,
            self.values,
        )
        return payload


@dataclass
class TimeState:
    """Resolve NMEA time-only fields to UTC timestamps."""

    current_date: date | None
    latest_timestamp: float | None = None
    latest_timestamp_message_index: int | None = None
    latest_time_of_day: time | None = None

    def update_date(self, value: Any) -> None:
        parsed_date = parse_nmea_date(value)
        if parsed_date is not None:
            self.current_date = parsed_date

    def timestamp(
        self,
        time_value: Any,
        date_value: Any = None,
        message_index: int | None = None,
    ) -> float | None:
        parsed_date = parse_nmea_date(date_value)
        if parsed_date is not None:
            self.current_date = parsed_date

        parsed_time = parse_nmea_time(time_value)
        if parsed_time is None or self.current_date is None:
            return None

        # Time-only sentences such as GGA carry no date. A large backwards
        # jump in time of day without a fresh date means UTC midnight passed.
        previous_time = self.latest_time_of_day
        if (
            parsed_date is None
            and previous_time is not None
            and seconds_of_day(previous_time) - seconds_of_day(parsed_time) > DAY_ROLLOVER_SECONDS
        ):
            self.current_date += timedelta(days=1)
        self.latest_time_of_day = parsed_time

        timestamp = datetime.combine(self.current_date, parsed_time, tzinfo=timezone.utc).timestamp()
        self.remember(timestamp, message_index)
        return timestamp

    def remember(self, timestamp: float, message_index: int | None) -> None:
        self.latest_timestamp = timestamp
        self.latest_timestamp_message_index = message_index

    def timestamp_for_untimed_message(self, message_index: int) -> float | None:
        if self.latest_timestamp is None or self.latest_timestamp_message_index is None:
            return None
        if message_index - self.latest_timestamp_message_index > MAX_UNTIMED_MESSAGE_DISTANCE:
            return None
        return self.latest_timestamp


class Parser(DataParser):
    name = "GNSS log via pyubx2"
    priority = 100
    extensions = frozenset({".log", ".txt", ".nmea", ".ubx"})
    format_name = GNSS_FORMAT_NAME

    def can_parse(self, input_path: str | Path) -> bool:
        """Return true when the file looks like GNSS data pyubx2 can handle."""

        path = Path(input_path)
        try:
            with path.open("rb") as file:
                sample = file.read(8192)
        except OSError:
            return False

        if NMEA_START_RE.search(sample):
            return True
        if sample_contains_ubx_nav_pvt_frame(sample):
            return True
        return False

    def parse(
        self,
        input_path: str | Path,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Import a GNSS stream into numeric time series."""

        path = Path(input_path)
        fallback_date = parse_date_from_filename(path.name)
        time_state = TimeState(current_date=fallback_date)
        series = self.build_series_catalog()
        progress_reporter = ProgressReporter(progress_callback)
        message_counts: Counter[str] = Counter()
        parse_errors = 0
        decode_errors = 0

        for message_index, (parsed_message, progress) in enumerate(iter_pyubx2_messages(path), start=1):
            progress_reporter.report(progress)
            if isinstance(parsed_message, UnicodeDecodeError):
                decode_errors += 1
                continue
            identity = str(getattr(parsed_message, "identity", "")).upper()
            if not identity:
                continue

            message_counts[identity] += 1
            message_kind = identity[-3:]

            try:
                if hasattr(parsed_message, "date"):
                    time_state.update_date(getattr(parsed_message, "date"))

                timestamp = time_state.timestamp(
                    getattr(parsed_message, "time", None),
                    getattr(parsed_message, "date", None),
                    message_index,
                )

                if identity == "NAV-PVT":
                    timestamp = parse_ubx_nav_pvt_timestamp(parsed_message)
                    if timestamp is not None:
                        time_state.remember(timestamp, message_index)
                    add_ubx_nav_pvt_series(series, timestamp, parsed_message)

                if message_kind in {"RMC", "GGA", "GLL"}:
                    add_position_series(series, timestamp, parsed_message)

                if message_kind == "RMC":
                    add_value(series, "speed_mps", timestamp, knots_to_mps(getattr(parsed_message, "spd", None)))
                    add_value(series, "course_deg", timestamp, getattr(parsed_message, "cog", None))

                if message_kind == "GGA":
                    add_value(series, "altitude_m", timestamp, getattr(parsed_message, "alt", None))
                    add_value(series, "geoid_separation_m", timestamp, getattr(parsed_message, "sep", None))
                    add_value(series, "satellites_used", timestamp, getattr(parsed_message, "numSV", None))
                    add_value(series, "fix_quality", timestamp, getattr(parsed_message, "quality", None))
                    add_value(series, "hdop", timestamp, getattr(parsed_message, "HDOP", None))

                if message_kind == "VTG":
                    timestamp = timestamp or time_state.timestamp_for_untimed_message(message_index)
                    add_value(series, "speed_mps", timestamp, kmh_to_mps(getattr(parsed_message, "sogk", None)))
                    add_value(series, "course_deg", timestamp, getattr(parsed_message, "cogt", None))

                if message_kind == "GSA":
                    timestamp = timestamp or time_state.timestamp_for_untimed_message(message_index)
                    add_value(series, "pdop", timestamp, getattr(parsed_message, "PDOP", None))
                    add_value(series, "hdop", timestamp, getattr(parsed_message, "HDOP", None))
                    add_value(series, "vdop", timestamp, getattr(parsed_message, "VDOP", None))
            except (TypeError, ValueError, OverflowError):
                parse_errors += 1

        populated_series = {
            builder.definition.key: builder.to_dict()
            for builder in series.values()
            if builder.values
        }
        skipped_timestamps = filter_gnss_time_outliers(populated_series)
        add_gnss_distance_series(self, populated_series)
        if not populated_series:
            seen = ", ".join(f"{name}: {count}" for name, count in sorted(message_counts.items()))
            detail = f" Parsed messages: {seen}." if seen else ""
            raise ValueError(f"GNSS stream contains no supported position or navigation data.{detail}")

        progress_reporter.finish()
        return {
            "format": self.format_name,
            "series": populated_series,
            "metadata": {
                "fallback_date": fallback_date.isoformat() if fallback_date else None,
                "message_counts": dict(sorted(message_counts.items())),
                "parse_errors": parse_errors,
                "decode_errors": decode_errors,
                "timestamp_outlier_samples_skipped": skipped_timestamps,
            },
        }

    def build_series_catalog(self) -> dict[str, SeriesBuilder]:
        series = {
            definition.name: SeriesBuilder(definition, self)
            for definition in self.series_definitions
        }
        if not series:
            raise ValueError(f"{self.config_path} must define at least one series.")
        return series


def iter_pyubx2_messages(input_path: Path) -> Iterable[tuple[Any, float]]:
    """Yield messages, or a UnicodeDecodeError for a damaged sentence."""

    try:
        from pyubx2 import (
            ERR_IGNORE,
            NMEA_PROTOCOL,
            RTCM3_PROTOCOL,
            UBX_PROTOCOL,
            UBXReader,
        )
    except ImportError as exc:
        raise RuntimeError(
            "pyubx2 is required for GNSS import. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        ) from exc

    protocol_filter = NMEA_PROTOCOL | UBX_PROTOCOL | RTCM3_PROTOCOL
    file_size = max(1, input_path.stat().st_size)

    with input_path.open("rb") as stream:
        reader = UBXReader(
            stream,
            protfilter=protocol_filter,
            quitonerror=ERR_IGNORE,
        )
        while True:
            before = stream.tell()
            try:
                _raw_data, parsed_message = next(reader)
            except StopIteration:
                break
            except UnicodeDecodeError as error:
                # Some pyubx2 versions decode NMEA outside their normal error
                # handling. The damaged sentence has already been consumed;
                # resume at the next frame without disabling checksums or
                # stripping bytes from legitimate UBX/RTCM binary messages.
                if stream.tell() <= before:
                    raise
                parsed_message = error
            yield parsed_message, min(1.0, stream.tell() / file_size)


def sample_contains_ubx_nav_pvt_frame(sample: bytes) -> bool:
    """Return true when a complete, checksummed UBX NAV-PVT frame is present."""

    offset = sample.find(b"\xb5\x62\x01\x07")
    while offset >= 0:
        if offset + 8 > len(sample):
            return False
        payload_length = int.from_bytes(sample[offset + 4:offset + 6], "little")
        frame_end = offset + 8 + payload_length
        if 84 <= payload_length <= 128 and frame_end <= len(sample):
            checksum_data = sample[offset + 2:offset + 6 + payload_length]
            if ubx_checksum(checksum_data) == tuple(sample[offset + 6 + payload_length:frame_end]):
                return True
        offset = sample.find(b"\xb5\x62\x01\x07", offset + 1)
    return False


def ubx_checksum(data: bytes) -> tuple[int, int]:
    checksum_a = 0
    checksum_b = 0
    for byte in data:
        checksum_a = (checksum_a + byte) & 0xFF
        checksum_b = (checksum_b + checksum_a) & 0xFF
    return checksum_a, checksum_b


def parse_ubx_nav_pvt_timestamp(message: Any) -> float | None:
    if not bool(getattr(message, "validDate", 0)) or not bool(getattr(message, "validTime", 0)):
        return None
    try:
        second = int(getattr(message, "second"))
        leap_second = second == 60
        base = datetime(
            int(getattr(message, "year")),
            int(getattr(message, "month")),
            int(getattr(message, "day")),
            int(getattr(message, "hour")),
            int(getattr(message, "min")),
            min(second, 59),
            tzinfo=timezone.utc,
        )
        nanoseconds = int(getattr(message, "nano", 0))
        return (base + timedelta(seconds=int(leap_second), microseconds=nanoseconds / 1000)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def add_ubx_nav_pvt_series(
    series: dict[str, SeriesBuilder],
    timestamp: float | None,
    message: Any,
) -> None:
    add_value(series, "satellites_used", timestamp, getattr(message, "numSV", None))
    # UBX fixType uses different codes from NMEA GGA quality. Keep a separate
    # series so mixed streams cannot label a 3D fix as PPS or time-only as RTK.
    add_value(series, "fix_type", timestamp, getattr(message, "fixType", None))
    add_value(series, "pdop", timestamp, getattr(message, "pDOP", None))
    if bool(getattr(message, "invalidLlh", 0)) or not bool(getattr(message, "gnssFixOk", 1)):
        return

    add_value(series, "latitude_deg", timestamp, getattr(message, "lat", None))
    add_value(series, "longitude_deg", timestamp, getattr(message, "lon", None))
    add_value(series, "altitude_m", timestamp, millimetres_to_metres(getattr(message, "hMSL", None)))
    add_value(series, "ehpe_m", timestamp, millimetres_to_metres(getattr(message, "hAcc", None)))
    add_value(series, "speed_mps", timestamp, millimetres_to_metres(getattr(message, "gSpeed", None)))
    add_value(series, "course_deg", timestamp, getattr(message, "headMot", None))


def add_position_series(
    series: dict[str, SeriesBuilder],
    timestamp: float | None,
    message: Any,
) -> None:
    add_value(series, "latitude_deg", timestamp, coordinate_to_decimal(getattr(message, "lat", None), getattr(message, "NS", None)))
    add_value(series, "longitude_deg", timestamp, coordinate_to_decimal(getattr(message, "lon", None), getattr(message, "EW", None)))


def add_value(series: dict[str, SeriesBuilder], name: str, timestamp: float | None, value: Any) -> None:
    builder = series.get(name)
    if builder is not None:
        builder.add(timestamp, value)


def parse_date_from_filename(filename: str) -> date | None:
    match = FILENAME_DATE_RE.search(filename)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_nmea_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None
    if "-" in text:
        return parse_iso_date(text)

    digits = re.sub(r"\D", "", text)
    if len(digits) < 6:
        return None
    digits = digits[:6]
    day = int(digits[0:2])
    month = int(digits[2:4])
    year_two_digits = int(digits[4:6])
    year = 2000 + year_two_digits if year_two_digits < 80 else 1900 + year_two_digits
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_nmea_time(value: Any) -> time | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.timetz().replace(tzinfo=None)
    if isinstance(value, time):
        return value.replace(tzinfo=None)

    text = str(value).strip()
    if not text:
        return None

    if ":" in text:
        try:
            return time.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            return None

    if "." in text:
        head, fraction = text.split(".", 1)
    else:
        head, fraction = text, ""
    digits = re.sub(r"\D", "", head).zfill(6)
    if len(digits) < 6:
        return None

    hours = int(digits[0:2])
    minutes = int(digits[2:4])
    seconds = int(digits[4:6])
    microseconds = 0
    if fraction:
        microseconds = int((re.sub(r"\D", "", fraction) + "000000")[:6])

    try:
        return time(hours, minutes, seconds, microseconds)
    except ValueError:
        return None


def seconds_of_day(value: time) -> float:
    return value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1_000_000


def coordinate_to_decimal(value: Any, hemisphere: Any = None) -> float | None:
    numeric = to_float(value)
    if numeric is None:
        return None

    if abs(numeric) > 180:
        degrees = math.floor(abs(numeric) / 100)
        minutes = abs(numeric) - degrees * 100
        numeric = degrees + minutes / 60

    hemisphere_text = str(hemisphere or "").strip().upper()
    if hemisphere_text in {"S", "W"}:
        numeric = -abs(numeric)
    return numeric


def knots_to_mps(value: Any) -> float | None:
    numeric = to_float(value)
    if numeric is None:
        return None
    return numeric * 0.514444


def kmh_to_mps(value: Any) -> float | None:
    numeric = to_float(value)
    if numeric is None:
        return None
    return numeric / 3.6


def millimetres_to_metres(value: Any) -> float | None:
    numeric = to_float(value)
    if numeric is None:
        return None
    return numeric / 1000


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")

    text = str(value).strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None
