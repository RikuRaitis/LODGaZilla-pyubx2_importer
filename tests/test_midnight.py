"""NMEA timestamp rollover regressions."""
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from pyubx2_importer.importer import Parser as GnssParser, TimeState


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def nmea(body):
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def gga(time_text):
    return nmea(f"GNGGA,{time_text},6016.3369830,N,02458.3666919,E,1,12,0.8,10.0,M,18.0,M,,")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class NmeaMidnightRolloverTests(unittest.TestCase):
    def test_time_only_sentences_advance_date_at_utc_midnight(self):
        state = TimeState(current_date=date(2026, 1, 3))
        self.assertEqual(state.timestamp("235959.00"), utc(2026, 1, 3, 23, 59, 59))
        self.assertEqual(state.timestamp("000000.00"), utc(2026, 1, 4, 0, 0, 0))
        self.assertEqual(state.timestamp("000001.00"), utc(2026, 1, 4, 0, 0, 1))

    def test_small_backward_step_and_fresh_date_do_not_advance(self):
        state = TimeState(current_date=date(2026, 1, 3))
        state.timestamp("120000.00")
        self.assertEqual(state.timestamp("115959.00"), utc(2026, 1, 3, 11, 59, 59))
        state.timestamp("235959.00")
        # A dated sentence after midnight sets the date itself; no extra day.
        self.assertEqual(state.timestamp("000000.00", "040126"), utc(2026, 1, 4))
        self.assertEqual(state.timestamp("000001.00"), utc(2026, 1, 4, 0, 0, 1))

    def test_gga_only_log_crossing_midnight_stays_monotonic(self):
        content = b"".join(gga(t) for t in ("235958.00", "235959.00", "000000.00", "000001.00"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "2026_01_03.log"
            path.write_bytes(content)
            result = GnssParser(PLUGIN_DIR / "importer.json").parse(path)
        times = np.asarray(result["series"]["GNSS/latitude_deg"]["time"], dtype=float)
        np.testing.assert_allclose(times, utc(2026, 1, 3, 23, 59, 58) + np.arange(4))
