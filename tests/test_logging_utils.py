"""Tests del formateador de logs con zona horaria configurable (reemplaza el reloj local/UTC)."""
import logging
import unittest
import zoneinfo

from src.gate.logging_utils import TzFormatter

# 2026-10-09 17:00:00 UTC
EPOCH_OCT_09_1700_UTC = 1791565200.0


def make_record(created: float) -> logging.LogRecord:
    record = logging.LogRecord("gate.test", logging.INFO, __file__, 1, "hola", None, None)
    record.created = created
    record.msecs = 0
    return record


class TestTzFormatter(unittest.TestCase):
    def test_formats_in_chicago_time_with_dst_abbreviation(self):
        fmt = TzFormatter("%(asctime)s %(message)s", zoneinfo.ZoneInfo("America/Chicago"))
        self.assertEqual(fmt.format(make_record(EPOCH_OCT_09_1700_UTC)), "2026-10-09 12:00:00,000 CDT hola")

    def test_formats_in_chicago_standard_time_in_winter(self):
        fmt = TzFormatter("%(asctime)s", zoneinfo.ZoneInfo("America/Chicago"))
        winter = EPOCH_OCT_09_1700_UTC + 60 * 24 * 3600  # 2026-12-08 17:00 UTC
        self.assertEqual(fmt.format(make_record(winter)), "2026-12-08 11:00:00,000 CST")

    def test_costa_rica_is_fixed_utc_minus_6(self):
        fmt = TzFormatter("%(asctime)s", zoneinfo.ZoneInfo("America/Costa_Rica"))
        self.assertEqual(fmt.format(make_record(EPOCH_OCT_09_1700_UTC)), "2026-10-09 11:00:00,000 CST")


if __name__ == "__main__":
    unittest.main()
