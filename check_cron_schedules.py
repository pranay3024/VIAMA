"""
Guard against a cron schedule that Vercel will refuse to deploy.

Vercel Hobby accounts may only run cron jobs **once per day**. A schedule that
fires more often than that is rejected at deploy time with:

    Hobby accounts are limited to daily cron jobs. This cron expression
    (0 */5 * * *) would run more than once per day. Upgrade to the Pro plan to
    unlock all Cron Jobs features on Vercel.

The nasty part is what happens next: a GitHub-triggered deployment that fails
this validation is dropped **silently**. Nothing appears in the Deployments
list, nothing appears in the Activity log, and a manually triggered Deploy Hook
answers `{"job": {"state": "PENDING"}}` while producing no build at all. The
production domain keeps serving the previous deployment, so the symptom is
"the portal is still on the old code" rather than an obvious error.

This script is that missing failure. It parses `vercel.json` and asserts every
cron expression fires at most once a day, and that every cron path still has a
route in the source tree.

    python check_cron_schedules.py

Exit 0 when the configuration is deployable, 1 when it is not. Worth running
before any deploy that touched `vercel.json`.

It reads JSON and source text rather than importing the app, so it needs no
database, no Flask app context and no environment.
"""

import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
VERCEL_JSON = os.path.join(ROOT, "vercel.json")

#: Vercel allows 100 cron jobs per project on every plan now, but on Hobby
#: each one may only fire once per day.
MAX_FIRES_PER_DAY = 1

#: `routes/` is where every portal route is declared.
ROUTES_DIR = os.path.join(ROOT, "routes")

#: Days in each month, for the day-of-month field.
MONTH_LENGTHS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class BadSchedule(ValueError):
    """The expression is not a cron expression Vercel accepts."""


def parse_field(field, low, high):
    """Every value a single cron field can take, as a sorted list of ints.

    Supports `*`, `5`, `1-9`, `*/15`, `1-9/2` and comma-separated lists of
    those. Vercel does not accept `MON`/`SUN` style names, so neither do we.
    """

    values = set()

    for part in field.split(","):
        part = part.strip()

        if not part:
            raise BadSchedule("empty field entry")

        if part == "*":
            values.update(range(low, high + 1))
            continue

        step = 1

        if "/" in part:
            part, _, step_text = part.partition("/")

            try:
                step = int(step_text)
            except ValueError:
                raise BadSchedule("bad step %r" % step_text)

            if step < 1:
                raise BadSchedule("step must be 1 or more")

            if not part:
                raise BadSchedule("step without a range")

        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, _, end_text = part.partition("-")

            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise BadSchedule("bad range %r" % part)
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise BadSchedule("bad value %r" % part)

        if start < low or end > high or start > end:
            raise BadSchedule("out of range: %r" % part)

        values.update(range(start, end + 1, step))

    return sorted(values)


def fires_per_day(expression):
    """How many times in a day this expression can fire.

    The answer is the number of matching (minute, hour) pairs on a date the
    expression can reach at all. An over-estimate is safe: the point is to
    reject anything that *might* run more than once a day.
    """

    fields = expression.split()

    if len(fields) != 5:
        raise BadSchedule(
            "expected 5 fields (minute hour day month weekday), got %d"
            % len(fields)
        )

    minute, hour, day, month, weekday = fields

    minutes = parse_field(minute, 0, 59)
    hours = parse_field(hour, 0, 23)
    days = parse_field(day, 1, 31)
    months = parse_field(month, 1, 12)
    weekdays = parse_field(weekday, 0, 7)

    if day != "*" and weekday != "*":
        raise BadSchedule(
            "day-of-month and day-of-week cannot both be set"
        )

    # Weekday 7 and 0 are both Sunday.
    weekdays = {0 if value == 7 else value for value in weekdays}

    reaches_a_day = False

    for month_value in months:
        month_length = MONTH_LENGTHS[month_value - 1]

        for day_value in days:
            if day_value > month_length:
                continue

            if weekday != "*":
                # 2024 is a leap year, so 29 February exists.
                date_weekday = date(2024, month_value, day_value).weekday()

                if date_weekday not in weekdays:
                    continue

            reaches_a_day = True
            break

        if reaches_a_day:
            break

    if not reaches_a_day:
        # The expression can never fire at all - useless, but not a deploy
        # blocker.
        return 0

    return len(minutes) * len(hours)


def route_exists(path):
    """Whether any file under routes/ declares this path."""

    needle = '"%s"' % path

    for name in sorted(os.listdir(ROUTES_DIR)):
        if not name.endswith(".py"):
            continue

        with open(os.path.join(ROUTES_DIR, name), encoding="utf-8") as handle:
            source = handle.read()

        if needle in source:
            return True

    return False


def main():
    problems = []

    if not os.path.exists(VERCEL_JSON):
        print("vercel.json not found at %s" % VERCEL_JSON)
        return 1

    with open(VERCEL_JSON, encoding="utf-8") as handle:
        try:
            config = json.load(handle)
        except ValueError as exc:
            print("vercel.json is not valid JSON: %s" % exc)
            return 1

    crons = config.get("crons") or []

    if not crons:
        print("vercel.json declares no cron jobs.")
        return 0

    print("Checking %d cron job(s) against Vercel's daily-only limit:\n" % len(crons))

    for entry in crons:
        path = entry.get("path")
        schedule = entry.get("schedule")

        if not path or not schedule:
            problems.append("a cron entry is missing its path or schedule")
            continue

        try:
            count = fires_per_day(schedule)
        except BadSchedule as exc:
            problems.append("%s: %s is not a valid cron expression (%s)" % (path, schedule, exc))
            continue

        if count > MAX_FIRES_PER_DAY:
            problems.append(
                "%s: %s fires %d times a day - Vercel Hobby rejects this and "
                "drops the deployment silently" % (path, schedule, count)
            )
            print("  FAIL  %-38s %-12s %d fires/day" % (path, schedule, count))
            continue

        if not route_exists(path):
            problems.append("%s: no route in routes/ declares this path" % path)
            print("  FAIL  %-38s %-12s no matching route" % (path, schedule))
            continue

        print("  ok    %-38s %-12s %d fires/day" % (path, schedule, count))

    print("")

    if problems:
        print("Cron configuration is NOT deployable:")
        for problem in problems:
            print("  - %s" % problem)
        return 1

    print("Cron configuration is deployable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
