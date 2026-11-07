"""Regression tests for standalone slicer time comments.

Run with: python -m unittest discover -s tests -v
The metadata parser requires Pillow, as it does in Moonraker.
"""

import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "components/file_manager/metadata.py"
SPEC = importlib.util.spec_from_file_location("gcode_metadata", SOURCE)
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)


class EstimatedTimeTest(unittest.TestCase):
    def parse(self, data, slicer_class=metadata.PrusaSlicer):
        return slicer_class("fixture.gcode", len(data), data).parse_estimated_time()

    def test_embedded_template_does_not_hide_real_estimate(self):
        data = (
            "; file_start_gcode = ; estimated printing time (normal mode) = "
            "{print_time_day}d {print_time_hour}h "
            "{print_time_minute}m {print_time_sec}s\n"
            "; estimated printing time (normal mode) = 25m 43s\n"
        )
        self.assertEqual(self.parse(data), 1543)

    def test_embedded_numeric_comment_is_not_an_estimate(self):
        data = (
            "; machine_start_gcode = ; estimated printing time = 9h\n"
            "; estimated printing time (normal mode) = 25m 43s\n"
        )
        self.assertEqual(self.parse(data), 1543)

    def test_template_without_standalone_estimate_is_missing(self):
        data = "; file_start_gcode = ; estimated printing time = {print_time_sec}s\n"
        self.assertIsNone(self.parse(data))

    def test_supported_time_units(self):
        for value, expected in [
            ("43s", 43),
            ("25m 43s", 1543),
            ("2h 3m 4s", 7384),
            ("1d 2h 3m 4s", 93784),
            ("0d 0h 25m 43s", 1543),
            ("0s", 0),
        ]:
            with self.subTest(value=value):
                self.assertEqual(
                    self.parse("; estimated printing time (normal mode) = " + value + "\n"),
                    expected,
                )

    def test_indented_standalone_comment_and_crlf(self):
        self.assertEqual(
            self.parse("\t  ; estimated printing time (normal mode) = 25m 43s\r\n"),
            1543,
        )

    def test_first_standalone_mode_remains_selected(self):
        data = (
            "; estimated printing time (normal mode) = 25m 43s\n"
            "; estimated printing time (silent mode) = 40m\n"
        )
        self.assertEqual(self.parse(data), 1543)

    def test_bambu_header_estimate_still_works(self):
        self.assertEqual(
            self.parse("; estimated printing time (normal mode) = 25m 43s\n",
                       metadata.BambuStudio),
            1543,
        )

    def test_missing_estimate(self):
        self.assertIsNone(self.parse("; layer_height = 0.2\n"))


if __name__ == "__main__":
    unittest.main()
