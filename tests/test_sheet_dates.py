from datetime import date, datetime, timezone

import pytest

from line_assistant.adapters import BANGKOK
from line_assistant.sheet_dates import date_to_serial, serial_to_date


@pytest.mark.parametrize("day,serial", [(date(1899, 12, 30), 0), (date(1900, 1, 1), 2),
                                       (date(1900, 3, 1), 61)])
def test_google_documented_serial_date_epoch(day, serial):
    assert date_to_serial(day) == serial
    assert serial_to_date(serial) == day


def test_bangkok_day_is_preserved_when_workbook_has_los_angeles_timezone():
    instant = datetime(2026, 12, 31, 18, tzinfo=timezone.utc)
    day = instant.astimezone(BANGKOK).date()
    assert serial_to_date(date_to_serial(day)) == date(2027, 1, 1)


def test_timestamp_and_fractional_serial_are_rejected():
    with pytest.raises(ValueError):
        date_to_serial(datetime(2026, 10, 11, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        serial_to_date(123.5)
