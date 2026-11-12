import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from pyubx2_importer.importer import Parser as GnssParser, ubx_checksum


PLUGIN_DIR = Path(__file__).resolve().parents[1]
DAY = 86400


def nmea(body):
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def rmc(time, date="230926"):
    return nmea(f"GNRMC,{time},A,6016.3369830,N,02458.3666919,E,0.018,,{date},,,A,V")


class GnssRecoveryTests(unittest.TestCase):
    def parse_log(self, content):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "2026_09_23.log"
            path.write_bytes(content)
            return GnssParser(PLUGIN_DIR / "importer.json").parse(path)
    def test_binary_damage_does_not_stop_later_sentences(self):
        damaged = b"$GNRMC,052755.60,A,6016.33\xe8\x00\xff\r\n"
        bad_checksum = rmc("052756.00").replace(b"6016", b"6017")
        result = self.parse_log(
            damaged + rmc("052755.00") + damaged + bad_checksum
            + b"\x00\xff\x01" + rmc("052757.00") + damaged
        )
        times = result["series"]["GNSS/latitude_deg"]["time"]
        self.assertEqual(len(times), 2)
        self.assertEqual(times[1] - times[0], 2)
        self.assertEqual(result["metadata"]["decode_errors"], 3)
    def test_valid_ubx_binary_survives_alongside_damaged_nmea(self):
        payload = bytearray(92)
        struct.pack_into("<H6B", payload, 4, 2026, 9, 23, 5, 27, 56, 3)
        payload[20:24] = bytes([3, 1, 0, 12])
        struct.pack_into("<ii", payload, 24, 249727784, 602722831)
        data = b"\x01\x07" + len(payload).to_bytes(2, "little") + payload
        frame = b"\xb5\x62" + data + bytes(ubx_checksum(data))
        gga = nmea("GNGGA,052755.00,6016.3369830,N,02458.3666919,E,4,12,0.87,33.006,M,17.713,M,,")
        result = self.parse_log(rmc("052755.00") + gga + b"$GNGGA,\xff\r\n" + frame)
        self.assertEqual(result["metadata"]["message_counts"]["NAV-PVT"], 1)
        latitudes = result["series"]["GNSS/latitude_deg"]["values"]
        self.assertEqual(len(latitudes), 3)
        self.assertAlmostEqual(latitudes[-1], 60.2722831)
        quality = result["series"]["GNSS/fix_quality"]
        fix_type = result["series"]["GNSS/fix_type"]
        np.testing.assert_array_equal(quality["values"], [4])
        np.testing.assert_array_equal(fix_type["values"], [3])
        self.assertEqual(quality["state_labels"]["4"], "RTK fixed")
        self.assertEqual(fix_type["state_labels"]["3"], "3D fix")
    def test_nmea_wrong_date_is_removed_from_all_channels(self):
        result = self.parse_log(rmc("052755.00") + rmc("053030.00", "010100") + rmc("052756.00"))
        for series in result["series"].values():
            self.assertEqual(len(series["time"]), 2)
            self.assertEqual(series["time"][1] - series["time"][0], 1)
        self.assertEqual(result["metadata"]["timestamp_outlier_samples_skipped"]["GNSS/latitude_deg"], 1)
    def test_ignored_message_is_not_counted_as_decode_error(self):
        from pyubx2_importer.importer import iter_pyubx2_messages

        def with_ignored_message(path):
            yield None, 0.0
            yield from iter_pyubx2_messages(path)

        with mock.patch("pyubx2_importer.importer.iter_pyubx2_messages", side_effect=with_ignored_message):
            result = self.parse_log(rmc("052755.00"))
        self.assertEqual(result["metadata"]["decode_errors"], 0)
