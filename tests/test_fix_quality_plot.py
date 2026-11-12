import os
import unittest
from pathlib import Path
import tempfile

import numpy as np
import pyqtgraph as pg

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PLUGIN_DIR = Path(__file__).resolve().parents[1]

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.gui.plots.event_state import (  # noqa: E402
    is_event_state_series,
    load_event_state_series,
)


class GnssCoordinateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_imported_fix_quality_has_named_states_in_cache_and_plot(self):
        from test_recovery import nmea
        from src.jobs.import_data import load_import_configuration, write_hdf5_cache

        config = load_import_configuration(PLUGIN_DIR / "importer.json")
        expected = ["GNSS fix", "Diff GNSS fix", "PPS fix", "RTK fixed", "RTK float"]
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "2026_09_23.nmea"
            source.write_bytes(b"".join(
                nmea(f"GNGGA,05275{quality}.00,6016.3369830,N,02458.3666919,E,{quality},12,0.87,33.006,M,17.713,M,,")
                for quality in range(1, 6)
            ))
            result = config.parser_instance.parse(source)
            cache = source.with_suffix(".h5")
            write_hdf5_cache(output_path=cache, input_path=source, selected_config=config, import_result=result)
            self.assertTrue(is_event_state_series(cache, "GNSS/fix_quality"))
            data = load_event_state_series(cache, "GNSS/fix_quality")
            np.testing.assert_array_equal(data["value"], [1, 2, 3, 4, 5])
            self.assertEqual([data["labels"][str(value)] for value in range(1, 6)], expected)
            from src.gui.plots.event_state import render_event_state_lane

            widget = pg.PlotWidget()
            widget.resize(1000, 200)
            widget.show()
            self.app.processEvents()
            try:
                items = render_event_state_lane(widget.getPlotItem(), 0, data, "#2196f3")
                self.assertEqual([item.toPlainText() for item in items if isinstance(item, pg.TextItem)], expected)
            finally:
                widget.close()
