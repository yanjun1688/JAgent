"""R8-a: shared lenient JSON parser for non-trusted LLM text.

Pins the fence-stripping and outermost-object extraction mechanics that five
call sites previously hand-copied, including the distinction between
"no object present" and "object present but malformed".
"""

from __future__ import annotations

import json

import pytest

from harness.core.lenient_json import LenientJsonError, parse_lenient_json, strip_code_fence


def test_plain_object_parses():
    assert parse_lenient_json('{"a": 1}') == {"a": 1}


def test_json_fenced_object_strips_tick_fence():
    text = "```json\n" + '{"a": 1}' + "\n```"
    assert parse_lenient_json(text) == {"a": 1}


def test_bare_fenced_object_without_language_tag():
    text = "```\n" + '{"a": 1}' + "\n```"
    assert parse_lenient_json(text) == {"a": 1}


def test_object_embedded_in_prose_is_extracted():
    text = 'Sure, here it is: {"a": 1} — hope that helps'
    assert parse_lenient_json(text) == {"a": 1}


def test_fenced_object_embedded_in_prose():
    text = 'prefix text\n```json\n{"a": 1}\n```\ntrailing'
    assert parse_lenient_json(text) == {"a": 1}


def test_non_object_json_is_returned_not_rejected():
    # A list is valid JSON; type validation is the caller's responsibility.
    assert parse_lenient_json("[1, 2, 3]") == [1, 2, 3]


def test_no_object_raises_no_object_kind():
    with pytest.raises(LenientJsonError) as exc:
        parse_lenient_json("there is no json here at all")
    assert exc.value.kind == "no_object"


def test_empty_and_none_raise_no_object():
    with pytest.raises(LenientJsonError) as exc:
        parse_lenient_json("")
    assert exc.value.kind == "no_object"
    with pytest.raises(LenientJsonError):
        parse_lenient_json(None)


def test_malformed_object_span_raises_invalid_kind_with_cause():
    with pytest.raises(LenientJsonError) as exc:
        parse_lenient_json('{"a": }')
    assert exc.value.kind == "invalid"
    assert isinstance(exc.value.cause, json.JSONDecodeError)


def test_prose_with_braces_but_undecodable_raises_invalid():
    with pytest.raises(LenientJsonError) as exc:
        parse_lenient_json("note {not valid json} done")
    assert exc.value.kind == "invalid"


def test_outermost_span_uses_first_and_last_brace():
    text = 'noise {"a": 1} trailing {"b": 2}'
    # First '{' to last '}' spans both objects — invalid as one JSON value.
    with pytest.raises(LenientJsonError) as exc:
        parse_lenient_json(text)
    assert exc.value.kind == "invalid"


def test_strip_code_fence_helper():
    assert strip_code_fence("```json\n{}\n```") == "{}"
    assert strip_code_fence("  {}  ") == "{}"
    assert strip_code_fence(None) == ""
    assert strip_code_fence("plain") == "plain"
