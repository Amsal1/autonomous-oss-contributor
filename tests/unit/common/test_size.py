"""Size classes and the numstat code/test split (DESIGN §9.14, decision B5)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ossbot.common.config import DEFAULT_EXCLUDE_GLOBS, DEFAULT_TEST_GLOBS
from ossbot.common.size import CLASS_BOUNDS, SIZE_CLASSES, classify, path_matches, split_numstat


@dataclass(frozen=True)
class E:
    path: str
    added: int | None
    deleted: int | None


@pytest.mark.parametrize(
    ("lines", "files", "expected"),
    [
        (0, 0, "xs"),
        (20, 2, "xs"),
        (21, 2, "s"),
        (20, 3, "s"),
        (80, 4, "s"),
        (81, 4, "m"),
        (250, 8, "m"),
        (251, 1, "l"),
        (500, 15, "l"),
        (500, 16, "xl"),
        (1000, 30, "xl"),
        (1001, 1, "xxl"),
        (1, 31, "xxl"),
    ],
)
def test_class_edges(lines: int, files: int, expected: str) -> None:
    assert classify(lines, files) == expected


def test_bounds_are_monotonic() -> None:
    bounds = list(CLASS_BOUNDS.values())
    assert bounds == sorted(bounds)
    assert tuple(CLASS_BOUNDS) == SIZE_CLASSES[:-1]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("tests/test_x.py", "test"),
        ("src/pkg/test/Foo.java", "test"),
        ("pkg/x_test.go", "test"),
        ("test_top.py", "test"),
        ("web/a.test.ts", "test"),
        ("web/a.spec.js", "test"),
        ("web/__tests__/a.js", "test"),
        ("package-lock.json", "excluded"),
        ("sub/Cargo.lock", "excluded"),
        ("go.sum", "excluded"),
        ("vendor/lib/x.go", "excluded"),
        ("ui/__snapshots__/a.snap", "excluded"),
        ("dist/app.min.js", "excluded"),
        ("api/x.pb.go", "excluded"),
        ("db/schema_generated.ts", "excluded"),
        ("src/main.py", "code"),
        ("src/testing.py", "code"),
        ("docs/contest.md", "code"),
    ],
)
def test_default_globs(path: str, kind: str) -> None:
    excluded = path_matches(path, DEFAULT_EXCLUDE_GLOBS) is not None
    test = not excluded and path_matches(path, DEFAULT_TEST_GLOBS) is not None
    assert {"excluded": excluded, "test": test, "code": not (excluded or test)}[kind]


def test_excluded_wins_over_test() -> None:
    m = split_numstat([E("tests/vendor/x.py", 10, 1)], DEFAULT_TEST_GLOBS, DEFAULT_EXCLUDE_GLOBS)
    assert (m.files, m.code_lines, m.test_lines) == (1, 0, 0)


def test_binary_counts_as_file_no_lines() -> None:
    m = split_numstat([E("img.png", None, None)], DEFAULT_TEST_GLOBS, DEFAULT_EXCLUDE_GLOBS)
    assert (m.files, m.code_lines, m.test_lines) == (1, 0, 0)


_PATHS = st.sampled_from(
    ["src/a.py", "lib/b.go", "tests/t.py", "x_test.go", "package-lock.json", "vendor/v.c", "README.md", "a.spec.ts"]
)
_ENTRY = st.builds(E, _PATHS, st.integers(0, 5000) | st.none(), st.integers(0, 5000) | st.none())


@given(st.lists(_ENTRY, max_size=40))
def test_split_invariants(entries: list[E]) -> None:
    m = split_numstat(entries, DEFAULT_TEST_GLOBS, DEFAULT_EXCLUDE_GLOBS)
    assert m.files == len(entries)  # every path counts toward max_files (B5)
    kept = [e for e in entries if path_matches(e.path, DEFAULT_EXCLUDE_GLOBS) is None]
    total = sum((e.added or 0) + (e.deleted or 0) for e in kept)
    assert m.code_lines + m.test_lines == total  # nothing double-counted or lost
    assert min(m.code_added, m.code_deleted, m.test_added, m.test_deleted) >= 0
    tests_only = [e for e in kept if path_matches(e.path, DEFAULT_TEST_GLOBS)]
    assert m.test_lines == sum((e.added or 0) + (e.deleted or 0) for e in tests_only)
