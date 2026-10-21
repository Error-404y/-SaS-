from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo('Europe/Zurich')


def zurich_time(value):
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZURICH).strftime('%d.%m.%Y %H:%M:%S %Z')


class ZurichFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, ZURICH).strftime(datefmt or '%d.%m.%Y %H:%M:%S %Z')
