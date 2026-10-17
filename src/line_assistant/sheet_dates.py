"""Date-only serial values: encode the already chosen Bangkok calendar date."""

from datetime import date, timedelta

SHEETS_EPOCH = date(1899, 12, 30)


def date_to_serial(day: date) -> int:
    if type(day) is not date:
        raise ValueError("Choose an explicit calendar date before encoding")
    return (day - SHEETS_EPOCH).days


def serial_to_date(serial: int) -> date:
    if type(serial) is not int:
        raise ValueError("Date-only cells require integer day serials")
    return SHEETS_EPOCH + timedelta(days=serial)
