"""Human-facing desk timestamps use US Eastern Time, including daylight saving."""

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def eastern_time(stamp: datetime, *, seconds: bool = False) -> str:
    local = stamp.astimezone(EASTERN)
    clock = local.strftime("%I:%M:%S %p" if seconds else "%I:%M %p").lstrip("0")
    return f"{local:%Y-%m-%d} {clock} {local:%Z}"
