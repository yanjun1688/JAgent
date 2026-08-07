"""Regression guard: fmtkv() calls must use a single %s placeholder.

fmtkv() returns one formatted string, so a log statement like
`_log.info("a %s %s", fmtkv(...))` raises TypeError inside the logging
handler (msg % args arity mismatch), which propagates up through the
event pipeline and kills the run. This test statically scans every
`<logger>.<level>("<msg>", fmtkv(...))` call and asserts the literal
message contains exactly one %s.
"""

import pathlib
import re

HARNESS_ROOT = pathlib.Path(__file__).resolve().parents[1] / "harness"

_PAT = re.compile(
    r'_log_\w+\.(?:info|debug|warning|error)\(\s*"([^"]*%s[^"]*)",\s*fmtkv\('
)


def _fmtkv_log_calls():
    for f in sorted(HARNESS_ROOT.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for m in _PAT.finditer(text):
            line = text[: m.start()].count("\n") + 1
            yield f, line, m.group(1)


def test_fmtkv_log_calls_single_placeholder():
    offenders = []
    for f, line, msg in _fmtkv_log_calls():
        if msg.count("%s") != 1:
            offenders.append(f"{f}:{line}: {msg.count('%s')} placeholders -> {msg!r}")
    assert not offenders, (
        "fmtkv() returns a single string; format strings must use exactly one %s:\n"
        + "\n".join(offenders)
    )
