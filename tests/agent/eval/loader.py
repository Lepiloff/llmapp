"""Eval fixture loader.

Each fixture file is a JSON document with at minimum a ``name`` key.
The schema depends on what the fixture is exercising — validation
fixtures carry ``raw_draft`` + ``expected_sanitized_draft``, future
re-actualization fixtures will carry ``snapshot`` + ``enriched`` +
``expected_diff``, etc. The loader is intentionally schema-agnostic
and hands the raw dict to each test, which Pydantic-validates the
shape it cares about.
"""
from __future__ import annotations

import json
from pathlib import Path


_FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def load_fixtures(*, prefix: str = "") -> list[dict]:
    """Load every JSON fixture whose filename starts with ``prefix``.

    ``prefix='validate_'`` returns the validation pack; ``prefix=''``
    returns everything. Sorted by filename for stable test-id ordering
    in pytest output.
    """
    fixtures = []
    for path in sorted(_FIXTURE_ROOT.glob(f"{prefix}*.json")):
        with path.open() as f:
            data = json.load(f)
        data["_fixture_path"] = str(path)
        fixtures.append(data)
    return fixtures
