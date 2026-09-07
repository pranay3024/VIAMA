"""
Shared, dependency-free logic used by the JSON API.

This package imports nothing from `models`, `routes` or `extensions` at module
level, so it can be imported from anywhere without a circular import. (A few
functions do import them lazily inside the function body, which is safe.)

It is a *copy* of constants and derived-value logic that currently lives inline
in `routes/*.py`. The route files are intentionally left untouched (the API was
built additively), so if you change a business rule in one place you must change
it in the other. Every function here documents the route code it mirrors.

That duplication is the biggest maintenance risk in this package, so it is no
longer left to memory:

    python check_config_drift.py

parses the route files and fails if the epoch, export columns, statuses, states
or roles they hardcode have drifted from what this package says they are. Run it
before any deploy that touched routes/.
"""
