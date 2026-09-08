"""What may go in ``project.reporting_tz``, and why an offset may not.

A daily chart needs one answer to "when does a day end". Task-273's first offer was
``date(ts, :tz)`` with a fixed offset out of this column, and
``docs/analytics-design.md`` section 3.5 measured that answer wrong for half of every
year: SQLite ships no timezone database, its ``date()`` modifier takes a *fixed*
offset, and Central time is ``-06:00`` in winter and ``-05:00`` in summer. Either
constant misfiles late-evening work by a day for six months, which is when a good deal
of this project's work is recorded.

Section 6, item C is the correction: **the column holds an IANA zone name**
(``America/Chicago``), day bucketing happens in the API through ``zoneinfo``, and SQL
never names a day. This module is the enforcement of the first clause, and migration
``002`` is the same rule as a database trigger for a writer that never comes through
here.

The check is deliberately in two halves, because they can fail for different reasons:

* **Shape** — a fixed offset, or something that cannot be a zone name at all, is
  refused always. This is the failure item C exists for, and it needs no data to
  detect.
* **Existence** — the name resolves against the running interpreter's timezone
  database. Skipped, rather than failed, when there is no database to resolve
  against: ``zoneinfo`` falls back to the system zoneinfo on Unix and to the
  ``tzdata`` package on Windows, and a storage-layer guard that refuses every project
  on a machine missing that package would be a worse failure than the one it is
  guarding against.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional, Set

#: A fixed UTC offset in any of the forms somebody would plausibly type. These are
#: exactly what section 3.5 rejects, so they are named rather than left to the
#: existence check -- ``-06:00`` is not a zone name, and saying *why* it is refused is
#: the whole value of refusing it.
_OFFSET = re.compile(r"^[+-]\d{1,2}(:?\d{2})?$")

#: What an IANA name looks like: ASCII letters, digits and ``_ + - /``, no spaces.
#: ``UTC`` and ``America/Chicago`` pass; ``America/Chicago (CST)`` does not.
_ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+/-]*$")


class ReportingTimezoneError(ValueError):
    """``reporting_tz`` was given something that is not an IANA zone name."""


@lru_cache(maxsize=1)
def _known_zones() -> Optional[Set[str]]:
    """Every zone name this interpreter can resolve, or ``None`` if it has none.

    ``None`` is not "no zones exist"; it is "this machine cannot answer", which is a
    different thing and must not be turned into a refusal. Cached because
    ``available_timezones`` walks the timezone database and this runs on every project
    open.
    """
    try:
        from zoneinfo import available_timezones
    except ImportError:  # pragma: no cover - zoneinfo is stdlib from 3.9
        return None
    try:
        found = available_timezones()
    except Exception:  # noqa: BLE001 - a missing tzdata raises from several places
        return None
    return found or None


def check_reporting_tz(value: str) -> str:
    """Return ``value`` if it is an IANA zone name, or say what is wrong with it.

    Raises :class:`ReportingTimezoneError` with the section 3.5 argument in the
    message, because the person who typed ``-06:00`` typed it for a reason and needs
    to be told which reason is wrong.
    """
    name = (value or "").strip()
    if not name:
        raise ReportingTimezoneError(
            "reporting_tz must be an IANA zone name such as 'America/Chicago', or "
            "'UTC'. It was empty."
        )
    if _OFFSET.match(name):
        raise ReportingTimezoneError(
            f"reporting_tz must be an IANA zone name such as 'America/Chicago', not "
            f"the fixed offset {name!r}. An offset is correct for half of the year: "
            "Central time is -06:00 in winter and -05:00 in summer, so either "
            "constant misfiles late-evening work by a day for six months "
            "(docs/analytics-design.md section 3.5). Day bucketing happens in the "
            "API through zoneinfo, which needs the zone rather than one of its "
            "offsets."
        )
    if not _ZONE_NAME.match(name):
        raise ReportingTimezoneError(
            f"reporting_tz must be an IANA zone name such as 'America/Chicago'. "
            f"{name!r} is not one."
        )
    known = _known_zones()
    if known is not None and name not in known:
        raise ReportingTimezoneError(
            f"reporting_tz {name!r} is not a zone this machine's timezone database "
            "knows. Use an IANA name such as 'America/Chicago', or 'UTC'."
        )
    return name


__all__ = ["ReportingTimezoneError", "check_reporting_tz"]
