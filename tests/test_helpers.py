import unittest
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock



PLUGIN_DIR = Path(__file__).resolve().parents[1]

from pyubx2_importer.importer import (  # noqa: E402
    Parser as GnssParser,
    TimeState,
    coordinate_to_decimal,
    parse_date_from_filename,
    parse_nmea_date,
    parse_nmea_time,
    sample_contains_ubx_nav_pvt_frame,
    ubx_checksum,
)


GNSS_CONFIG_PATH = PLUGIN_DIR / "importer.json"


class GnssHelperTests(unittest.TestCase):
    def test_filename_date_parser_accepts_sample_style_name(self):
        self.assertEqual(
            parse_date_from_filename("2026_08_20_13_42_13.log"),
            date(2026, 8, 20),
        )

    def test_nmea_date_and_time_parsers(self):
        self.assertEqual(parse_nmea_date("200826"), date(2026, 8, 20))
        self.assertEqual(parse_nmea_time("104214.20"), time(10, 42, 14, 200000))

    def test_raw_nmea_coordinate_conversion(self):
        self.assertAlmostEqual(coordinate_to_decimal("6016.3328559", "N"), 60.272214265)
        self.assertAlmostEqual(coordinate_to_decimal("02458.3671857", "E"), 24.972786428)

    def test_detection_accepts_supported_ubx_and_rejects_pure_rtcm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ubx_path = Path(temp_dir) / "navigation.bin"
            rtcm_path = Path(temp_dir) / "sample.rtcm3"
            payload = bytes(92)
            checksum_data = b"\x01\x07" + len(payload).to_bytes(2, "little") + payload
            nav_pvt_frame = b"\xb5\x62" + checksum_data + bytes(ubx_checksum(checksum_data))
            ubx_path.write_bytes(nav_pvt_frame)
            rtcm_path.write_bytes(b"\xd3\x00\x01\x00")

            parser = GnssParser(GNSS_CONFIG_PATH)

            self.assertTrue(sample_contains_ubx_nav_pvt_frame(nav_pvt_frame))
            self.assertFalse(sample_contains_ubx_nav_pvt_frame(nav_pvt_frame[:-1] + b"\x00"))
            self.assertTrue(parser.can_parse(ubx_path))
            self.assertFalse(parser.can_parse(rtcm_path))

    def test_untimed_timestamp_is_not_reused_after_message_gap(self):
        state = TimeState(date(2026, 8, 20))
        timestamp = state.timestamp("104214.20", message_index=1)

        self.assertEqual(state.timestamp_for_untimed_message(21), timestamp)
        self.assertIsNone(state.timestamp_for_untimed_message(22))

    def test_nav_pvt_is_converted_to_existing_gnss_series(self):
        message = SimpleNamespace(
            identity="NAV-PVT",
            validDate=1,
            validTime=1,
            year=2026,
            month=8,
            day=20,
            hour=10,
            min=42,
            second=14,
            nano=250_000_000,
            invalidLlh=0,
            lat=60.2722142,
            lon=24.9727864,
            hMSL=12_345,
            hAcc=850,
            gSpeed=2_500,
            headMot=123.45,
            numSV=14,
            fixType=3,
            pDOP=1.25,
        )
        parser = GnssParser(GNSS_CONFIG_PATH)

        with mock.patch("pyubx2_importer.importer.iter_pyubx2_messages", return_value=iter([(message, 1.0)])):
            result = parser.parse(Path("sample.ubx"))

        expected_timestamp = datetime(2026, 8, 20, 10, 42, 14, 250000, tzinfo=timezone.utc).timestamp()
        series = result["series"]
        self.assertEqual(series["GNSS/latitude_deg"]["time"].tolist(), [expected_timestamp])
        self.assertAlmostEqual(series["GNSS/latitude_deg"]["values"][0], 60.2722142)
        self.assertAlmostEqual(series["GNSS/altitude_m"]["values"][0], 12.345)
        self.assertAlmostEqual(series["GNSS/ehpe_m"]["values"][0], 0.85)
        self.assertAlmostEqual(series["GNSS/speed_mps"]["values"][0], 2.5)

    def test_vtg_and_gsa_reuse_nearby_nmea_timestamp(self):
        rmc = SimpleNamespace(
            identity="GNRMC",
            date="200826",
            time="104214.20",
            lat="6016.3328559",
            NS="N",
            lon="02458.3671857",
            EW="E",
            spd=None,
            cog=None,
        )
        vtg = SimpleNamespace(identity="GNVTG", sogk=36.0, cogt=180.0)
        gsa = SimpleNamespace(identity="GNGSA", PDOP=1.2, HDOP=0.8, VDOP=0.9)
        parser = GnssParser(GNSS_CONFIG_PATH)

        messages = iter([(rmc, 0.3), (vtg, 0.6), (gsa, 1.0)])
        with mock.patch("pyubx2_importer.importer.iter_pyubx2_messages", return_value=messages):
            result = parser.parse(Path("sample.nmea"))

        expected_timestamp = datetime(2026, 8, 20, 10, 42, 14, 200000, tzinfo=timezone.utc).timestamp()
        series = result["series"]
        self.assertEqual(series["GNSS/speed_mps"]["time"].tolist(), [expected_timestamp])
        self.assertAlmostEqual(series["GNSS/speed_mps"]["values"][0], 10.0)
        self.assertEqual(series["GNSS/pdop"]["time"].tolist(), [expected_timestamp])

    def test_parse_rejects_stream_without_supported_series(self):
        parser = GnssParser(GNSS_CONFIG_PATH)
        unsupported = SimpleNamespace(identity="GNGSV")

        with mock.patch(
            "pyubx2_importer.importer.iter_pyubx2_messages",
            return_value=iter([(unsupported, 1.0)]),
        ):
            with self.assertRaisesRegex(ValueError, "no supported position or navigation data"):
                parser.parse(Path("sample.nmea"))
