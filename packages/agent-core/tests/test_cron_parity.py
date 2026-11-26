"""Cross-check the in-house cron evaluator against ``croniter`` (MIT).

``agent_core.scheduling.cron`` is a stdlib-only five-field parser. Its own
tests pin the semantics we care about (DST folds, POSIX day OR rule, strict
"after" boundary). This module adds a *differential* check against a mature,
widely deployed implementation so a divergence in the shared part of the
semantics (field expansion, step/range/list handling, the day-of-month /
day-of-week OR rule) is caught instead of discovered in production.

Only zones without DST transitions are compared: the two libraries make
different, deliberate choices for ambiguous local times (see the
``next_run_at`` docstring), and those choices are pinned by ``test_cron.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("croniter", reason="croniter (test extra) not installed")
from croniter import croniter as Croniter  # noqa: E402

from agent_core.scheduling.cron import next_run_at  # noqa: E402

# A Thursday morning in mid-January; six fires from here cover day, weekday
# and month boundaries for every expression below.
START = datetime(2026, 1, 15, 9, 41, tzinfo=timezone.utc)

# No DST in these zones, so local->UTC is monotonic and unambiguous.
ZONES = ["UTC", "Asia/Shanghai"]

EXPRESSIONS = [
    "* * * * *",
    "*/15 * * * *",
    "0 0 * * *",
    "30 2 1 * *",
    "0 12 * * 1-5",
    "0 12 */2 * *",
    "0 */6 * * *",
    "0 0 29 2 *",
    "0 0 1,15 * *",
    "5 4 * * 0",
    "45 23 * * 7",
    # Both day fields restricted -> a day matches if EITHER matches.
    "10 2 1 * 1",
    "0 3 20-25 * 5",
]


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("expr", EXPRESSIONS)
def test_next_runs_match_croniter(expr: str, zone: str) -> None:
    # croniter evaluates in the timezone of the *base* datetime, so hand it the
    # start instant converted into the target zone. Feed both sides the same
    # instant; compare in UTC so a different tzinfo representation is fine.
    base = START.astimezone(ZoneInfo(zone))
    ours = START
    reference = Croniter(expr, base)
    for _ in range(6):
        ours = next_run_at(expr, zone, ours)
        theirs = reference.get_next(datetime).astimezone(timezone.utc)
        assert ours == theirs, f"{expr!r} in {zone}: ours={ours} croniter={theirs}"


def test_invalid_expressions_agree() -> None:
    """Whatever croniter rejects must not be accepted by validate_cron either."""
    from agent_core.scheduling.cron import validate_cron

    for expr in ("", "* * * *", "60 * * * *", "* 24 * * *", "0 0 32 * *", "a * * * *"):
        with pytest.raises(Exception):
            Croniter(expr, START)
        assert validate_cron(expr) is not None, expr
