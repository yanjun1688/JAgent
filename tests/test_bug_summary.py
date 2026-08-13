"""Keep the Bug summary index synchronized with Bug reports."""

from __future__ import annotations

import re
from pathlib import Path


BUG_DIR = Path(__file__).parents[1] / "JAgent-docs" / "Bug"
SUMMARY = BUG_DIR / "JAGENT-Bug-Summary.md"
BUG_FILE_PATTERN = re.compile(r"^JAGENT-\d{4}-P[01]-\d{2}_.+\.md$")
LINK_PATTERN = re.compile(r"\]\(\./([^\)]+\.md)\)")


def test_bug_summary_links_every_bug_report_once():
    summary = SUMMARY.read_text(encoding="utf-8")
    reports = {path.name for path in BUG_DIR.iterdir() if BUG_FILE_PATTERN.match(path.name)}
    links = LINK_PATTERN.findall(summary)

    assert len(links) == len(set(links)), "Bug summary contains duplicate report links"
    assert set(links) == reports, f"Bug summary/report mismatch: links={set(links) ^ reports}"
    assert all((BUG_DIR / link).is_file() for link in links)


def test_bug_summary_counts_match_report_severity():
    summary = SUMMARY.read_text(encoding="utf-8")
    reports = [path.name for path in BUG_DIR.iterdir() if BUG_FILE_PATTERN.match(path.name)]
    for severity in ("P0", "P1"):
        expected = sum(f"-{severity}-" in name for name in reports)
        match = re.search(rf"\| {severity} \| (\d+) \|", summary)
        assert match, f"Missing {severity} count in summary"
        assert int(match.group(1)) == expected
