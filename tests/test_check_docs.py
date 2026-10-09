"""scripts/check_docs.py: reading the suite's count and the doc's."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parents[1] / "scripts" / "check_docs.py"
)
assert _spec and _spec.loader
check_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docs)


def test_reads_pytests_closing_line():
    out = "tests/test_a.py::test_x\n\n484 tests collected in 0.71s\n"
    assert check_docs.collected(out) == 484
    assert check_docs.collected("1 test collected in 0.1s\n") == 1
    with pytest.raises(ValueError):
        check_docs.collected("no tests ran\n")


def test_reads_and_rewrites_the_docs_count():
    text = "*Status: v0.9 — built, tested (1,476 tests), and running*"
    assert check_docs.stated(text) == 1476
    assert check_docs.with_count(text, 490) == (
        "*Status: v0.9 — built, tested (490 tests), and running*"
    )
    with pytest.raises(ValueError):
        check_docs.stated("no count here")


def test_the_doc_states_a_count():
    doc = check_docs.DOC.read_text()
    assert check_docs.stated(doc) > 0
