"""Eval pack — pytest plugin scaffolding.

Eval tests are *gated* behind ``--eval`` so they don't run in the
default ``pytest tests/`` sweep. The default flow:

* CI / dev runs: skip eval. The pack is real fixture data and its
  failures need eyes-on review (a 3pp accuracy drop after a prompt
  change isn't a code regression, it's a prompt-tuning signal).
* Operator run: ``pytest tests/agent/eval/ --eval`` exercises every
  fixture and prints a per-fixture pass/fail.

Validation-pack tests are pure (no LLM call): they replay saved LLM
raw output through the validation / sanitization pipeline and assert
the output still matches the saved expected sanitized shape. This
catches regressions in ``validate_enriched_draft``, slugify
behaviour, confidence thresholds, evidence-required rules, etc.
without burning API tokens.

A future ``--eval-llm`` extension can layer real-LLM regression
runs on top — those WILL cost real money (~$0.006/fixture at current
pricing) and need explicit operator intent, which is why they're a
separate flag.
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--eval",
        action="store_true",
        default=False,
        help="Run the agent eval pack (tests/agent/eval/).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--eval"):
        return
    skip_eval = pytest.mark.skip(
        reason="Eval pack — pass --eval to enable."
    )
    for item in items:
        if "tests/agent/eval/" in str(item.fspath):
            item.add_marker(skip_eval)
