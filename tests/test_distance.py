import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import h5py
import numpy as np

from pyubx2_importer.importer import Parser as GnssParser
from src.jobs.import_data import ImportConfiguration, SERIES_INDEX_ATTR, write_hdf5_cache


PLUGIN_DIR = Path(__file__).resolve().parents[1]
# At the equator, this longitude increment is a 3 m great-circle step.
THREE_METRES_DEG = math.degrees(3 / 6_371_008.8)


def nmea(body):
    checksum = 0
    for byte in body.encode("ascii"):
        checksum ^= byte
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


class GnssDistanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
    def assert_distances(self, result, horizontal, spatial):
        for name, expected in (("distance_2d_m", horizontal), ("distance_3d_m", spatial)):
            series = result["series"][f"GNSS/{name}"]
            np.testing.assert_allclose(series["values"], expected, atol=1e-6)
            self.assertEqual(series["unit"], "m")
            self.assertTrue(series["visible"])
            self.assertTrue(np.all(np.diff(series["time"]) > 0))
    def test_nmea_duplicates_and_timestamp_outliers_do_not_inflate_distance(self):
        content = b""
        for second, longitude, altitude, date in (
            (0, 0, 10, "230926"),
            (1, 1, 1000, "010100"),
            (2, THREE_METRES_DEG, 14, "230926"),
        ):
            lon = f"{longitude * 60:015.9f}"
            content += nmea(f"GNRMC,1200{second:02d}.00,A,0000.000000,N,{lon},E,0,0,{date},,,A")
            content += nmea(f"GNGGA,1200{second:02d}.00,0000.000000,N,{lon},E,4,12,1,{altitude},M,0,M,,")
        path = self.folder / "2026_09_23.nmea"
        path.write_bytes(content)
        parser = GnssParser(PLUGIN_DIR / "importer.json")
        result = parser.parse(path)
        self.assert_distances(result, [0, 3], [0, 5])
        self.assertEqual(len(result["series"]["GNSS/latitude_deg"]["time"]), 4)
        self.assert_cache_distances(parser, path, result)
    def test_ubx_nav_pvt_altitude_is_used_in_metres(self):
        messages = [SimpleNamespace(
            identity="NAV-PVT", validDate=1, validTime=1,
            year=2026, month=9, day=23, hour=12, min=0, second=i,
            lat=0, lon=i * THREE_METRES_DEG, hMSL=10_000 + i * 4_000,
        ) for i in range(3)]
        parser = GnssParser(PLUGIN_DIR / "importer.json")
        with mock.patch("pyubx2_importer.importer.iter_pyubx2_messages",
                        return_value=iter((message, 1.0) for message in messages)):
            result = parser.parse(self.folder / "track.ubx")
        self.assert_distances(result, [0, 3, 6], [0, 5, 10])
    def assert_cache_distances(self, parser, path, result):
        cache = path.with_suffix(".h5")
        write_hdf5_cache(
            output_path=cache, input_path=path,
            selected_config=ImportConfiguration(parser.config_path, parser.config, parser),
            import_result=result,
        )
        with h5py.File(cache, "r") as handle:
            index = json.loads(handle.attrs[SERIES_INDEX_ATTR])
            for name, expected in (("distance_2d_m", [0, 3]), ("distance_3d_m", [0, 5])):
                group = handle[f"series/GNSS/{name}"]
                np.testing.assert_allclose(group["value"][:], expected, atol=1e-6)
                np.testing.assert_array_equal(
                    group["time_unix"][:], result["series"][f"GNSS/{name}"]["time"],
                )
                self.assertEqual(group.attrs["unit"], "m")
                self.assertTrue(any(entry["navigator_name"] == name for entry in index))
