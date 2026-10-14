import datetime as dt


def parse_offset(tz: str) -> dt.timezone:
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4]) if len(digits) > 2 else 0
    return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))


def parse_pta_time(s: str, tz: str) -> dt.datetime:
    naive = dt.datetime.strptime(s, "%Y/%m/%d %H:%M:%S")
    return naive.replace(tzinfo=parse_offset(tz))


def to_iso_time(d: dt.datetime) -> str:
    if d.tzinfo is None:
        raise ValueError("to_iso_time requires an aware datetime")
    off = d.strftime("%z")
    tail = off[:3] + ":" + off[3:] if off else "+00:00"
    return d.strftime("%Y-%m-%dT%H:%M:%S") + tail


def to_reltime(td: dt.timedelta) -> str:
    # exact integer millisecond math (total_seconds() is float — imprecise)
    total_ms = ((td.days * 86400 + td.seconds) * 1000
                + td.microseconds // 1000)
    if total_ms < 0:
        return "-" + to_reltime(-td)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    base = f"{hours}:{minutes:02d}:{seconds:02d}"
    # whole seconds stay clean ("0:00:05"); non-zero ms append ".mmm"
    # (e.g. PTA problem time limits: 100 ms → "0:00:00.100")
    return f"{base}.{ms:03d}" if ms else base


def parse_reltime(s: str) -> dt.timedelta:
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    parts = s.split(":")
    if len(parts) == 3:
        hours, minutes = int(parts[0]), int(parts[1])
        # float seconds: parse_reltime(to_reltime(td)) must round-trip
        # millisecond values ("0:00:00.100" → 100 ms, not 0)
        seconds = float(parts[2])
    else:
        hours = 0
        minutes = int(parts[0])
        seconds = float(parts[1])
    td = dt.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    return -td if negative else td
