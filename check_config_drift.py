"""
Guard against core/config.py drifting away from routes/*.py.

`core/config.py` is a deliberate *copy* of business rules that still live inline
in the portal's route files - the API was built additively and the routes were
left untouched on purpose (see core/__init__.py). The cost of that decision is
that a rule changed in one place and not the other makes the portal and the API
quietly disagree, and nothing fails to tell you.

This script is that missing failure. It parses the route files and asserts the
constants they hardcode still match what core/config.py says they are.

    python check_config_drift.py

Exit 0 when they agree, 1 when they do not. Worth wiring into CI, or running
before any deploy that touched a route file.

It reads source with `ast` rather than importing the routes, so it needs no
database, no Flask app context and no environment.
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.path.join(ROOT, "routes")

sys.path.insert(0, ROOT)

from core.config import (  # noqa: E402
    ASSIGNMENT_STATUSES,
    EXPORT_COLUMNS,
    KNOWN_STATES,
    PROJECT_START,
    ROLES,
    STATE_TO_TEAM,
    SURVEY_STATUSES,
)

problems = []
checks_run = 0


def fail(check, where, detail):
    problems.append((check, where, detail))


def parsed_routes():
    """(filename, AST) for every module in routes/."""
    for name in sorted(os.listdir(ROUTES)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(ROUTES, name)
        with open(path, encoding="utf-8") as handle:
            yield name, ast.parse(handle.read(), filename=name)


def string_constants(node):
    """Every string literal directly inside `node`, flattening list/tuple/set."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = []
        for element in node.elts:
            out.extend(string_constants(element))
        return out
    return []


# ---------------------------------------------------------------------------
# 1. The project-week epoch
# ---------------------------------------------------------------------------

def check_project_start(trees):
    """Every `project_start = datetime(...)` must equal config.PROJECT_START."""
    global checks_run
    found = 0

    for name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "project_start"
                for t in node.targets
            ):
                continue
            call = node.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
                continue
            if call.func.id != "datetime":
                continue

            args = [a.value for a in call.args if isinstance(a, ast.Constant)]
            if len(args) < 3:
                continue

            found += 1
            actual = tuple(args[:3])
            expected = (PROJECT_START.year, PROJECT_START.month, PROJECT_START.day)

            if actual != expected:
                fail(
                    "PROJECT_START",
                    "routes/{}:{}".format(name, node.lineno),
                    "route says datetime{}, core.config.PROJECT_START is {}".format(
                        actual, PROJECT_START.date()
                    ),
                )

    checks_run += 1

    if not found:
        fail(
            "PROJECT_START",
            "routes/",
            "no `project_start = datetime(...)` found at all - the routes were "
            "refactored and this guard no longer checks anything",
        )
    return found


# ---------------------------------------------------------------------------
# 2. Export column order
# ---------------------------------------------------------------------------

def check_export_columns(trees):
    """The xlsx `headers = [...]` in admin.py must equal config.EXPORT_COLUMNS."""
    global checks_run
    found = 0

    for name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id == "headers" for t in node.targets
            ):
                continue

            values = string_constants(node.value)
            # Only the export header list, not any other local named `headers`.
            if "Date" not in values or "Cycle" not in values:
                continue

            found += 1

            if tuple(values) != tuple(EXPORT_COLUMNS):
                fail(
                    "EXPORT_COLUMNS",
                    "routes/{}:{}".format(name, node.lineno),
                    "route exports {}, core.config.EXPORT_COLUMNS is {}".format(
                        values, list(EXPORT_COLUMNS)
                    ),
                )

    checks_run += 1

    if not found:
        fail(
            "EXPORT_COLUMNS",
            "routes/",
            "no export `headers = [...]` found - this guard no longer checks "
            "anything",
        )
    return found


# ---------------------------------------------------------------------------
# 3. Vocabularies: states, statuses, roles
# ---------------------------------------------------------------------------

def _compared_strings(tree, attribute):
    """String literals a route compares against `<something>.<attribute>`.

    Catches both `Survey.status == "completed"` and
    `survey.state in ["ASSAM", "BIHAR"]`.
    """
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == attribute):
            continue
        for comparator in node.comparators:
            for value in string_constants(comparator):
                out.append((value, node.lineno))

    return out


def _keyword_strings(tree, keyword_name):
    """String literals passed as `<keyword_name>="..."` to any call."""
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != keyword_name:
                continue
            for value in string_constants(keyword.value):
                out.append((value, node.lineno))

    return out


def _session_role_strings(tree):
    """String literals compared against `session.get("role")`."""
    out = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        call = node.left
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != "get":
            continue
        if not any(
            isinstance(a, ast.Constant) and a.value == "role" for a in call.args
        ):
            continue
        for comparator in node.comparators:
            for value in string_constants(comparator):
                out.append((value, node.lineno))

    return out


def check_vocabulary(trees, label, collector, allowed, config_name):
    """Every literal the routes use for this field must be a known value."""
    global checks_run
    checks_run += 1
    seen = 0

    for name, tree in trees:
        for value, lineno in collector(tree):
            seen += 1
            if value not in allowed:
                fail(
                    label,
                    "routes/{}:{}".format(name, lineno),
                    "route uses {!r}, which is not in core.config.{} ({})".format(
                        value, config_name, ", ".join(sorted(allowed))
                    ),
                )

    return seen


# ---------------------------------------------------------------------------

def main():
    trees = list(parsed_routes())

    if not trees:
        print("No route modules found under {}".format(ROUTES))
        return 1

    print("Checking {} route module(s) against core/config.py\n".format(len(trees)))

    counts = {
        "project_start literals": check_project_start(trees),
        "export header lists": check_export_columns(trees),
        "survey status literals": check_vocabulary(
            trees,
            "SURVEY_STATUSES",
            lambda t: _compared_strings(t, "status") + _keyword_strings(t, "status"),
            set(SURVEY_STATUSES) | set(ASSIGNMENT_STATUSES),
            "SURVEY_STATUSES / ASSIGNMENT_STATUSES",
        ),
        "state literals": check_vocabulary(
            trees,
            "KNOWN_STATES",
            lambda t: _compared_strings(t, "state") + _keyword_strings(t, "state"),
            set(KNOWN_STATES) | set(STATE_TO_TEAM),
            "KNOWN_STATES",
        ),
        "role literals": check_vocabulary(
            trees,
            "ROLES",
            lambda t: _session_role_strings(t) + _keyword_strings(t, "role"),
            set(ROLES),
            "ROLES",
        ),
    }

    for label, count in counts.items():
        print("  {:<26} {} inspected".format(label, count))

    print()

    if problems:
        print("DRIFT DETECTED - {} problem(s):\n".format(len(problems)))
        for check, where, detail in problems:
            print("  [{}] {}".format(check, where))
            print("      {}\n".format(detail))
        print(
            "Fix by making the route and core/config.py agree. If the route is\n"
            "right, update core/config.py - the API is serving the stale value."
        )
        return 1

    print("OK - {} check(s) passed, no drift between routes/ and core/config.py".format(
        checks_run
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
