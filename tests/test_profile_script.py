"""Tests for scripts/telegram_profile.py. Pure functions only, no network."""

from __future__ import annotations

import pytest

from scripts.telegram_profile import (
    COMMANDS,
    DESCRIPTION,
    MAX_COMMAND_DESCRIPTION_LENGTH,
    MAX_COMMANDS,
    MAX_DESCRIPTION_LENGTH,
    MAX_SHORT_DESCRIPTION_LENGTH,
    SHORT_DESCRIPTION,
    ProfileState,
    ProfileValidationError,
    compare_commands,
    compute_diff,
    registered_command_names,
    validate_commands,
    validate_profile,
    validate_text,
)

FORBIDDEN_SAMPLES = [
    ("em-dash", "a \u2014 b"),
    ("en-dash", "a \u2013 b"),
    ("spaced-hyphen", "a - b"),
    ("tvna", "TvNa"),
    ("tv-na", "TV NA"),
    ("capstone", "capstone"),
    ("group-10", "Group 10"),
]


def _state(**overrides: object) -> ProfileState:
    base: dict[str, object] = {
        "description": "description",
        "short_description": "short",
        "commands": (("start", "Welcome"),),
    }
    base.update(overrides)
    return ProfileState(**base)  # type: ignore[arg-type]


# --- Validation: forbidden wording ----------------------------------------


@pytest.mark.parametrize(("label", "bad"), FORBIDDEN_SAMPLES, ids=lambda v: str(v))
def test_description_rejects_forbidden_wording(label: str, bad: str) -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_text("description", f"hello {bad} world", MAX_DESCRIPTION_LENGTH)
    assert excinfo.value.field == "description"
    assert "description" in str(excinfo.value)


@pytest.mark.parametrize(("label", "bad"), FORBIDDEN_SAMPLES, ids=lambda v: str(v))
def test_command_description_rejects_forbidden_wording(
    label: str, bad: str
) -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_commands((("start", f"hello {bad} world"),))
    assert excinfo.value.field == "commands[start].description"


def test_short_description_error_names_its_field() -> None:
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_text("short_description", "a - b", MAX_SHORT_DESCRIPTION_LENGTH)
    assert excinfo.value.field == "short_description"


# --- Validation: lengths ---------------------------------------------------


def test_description_length_limit() -> None:
    validate_text("description", "a" * MAX_DESCRIPTION_LENGTH, MAX_DESCRIPTION_LENGTH)
    with pytest.raises(ProfileValidationError):
        validate_text(
            "description",
            "a" * (MAX_DESCRIPTION_LENGTH + 1),
            MAX_DESCRIPTION_LENGTH,
        )


def test_short_description_length_limit() -> None:
    validate_text(
        "short_description",
        "a" * MAX_SHORT_DESCRIPTION_LENGTH,
        MAX_SHORT_DESCRIPTION_LENGTH,
    )
    with pytest.raises(ProfileValidationError):
        validate_text(
            "short_description",
            "a" * (MAX_SHORT_DESCRIPTION_LENGTH + 1),
            MAX_SHORT_DESCRIPTION_LENGTH,
        )


def test_command_count_limit() -> None:
    allowed = tuple((f"c{i}", "text") for i in range(MAX_COMMANDS))
    validate_commands(allowed)  # 100 is allowed
    too_many = allowed + (("extra", "text"),)
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_commands(too_many)
    assert excinfo.value.field == "commands"


def test_command_description_length_limit() -> None:
    at_limit = "a" * MAX_COMMAND_DESCRIPTION_LENGTH
    validate_commands((("start", at_limit),))
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_commands((("start", at_limit + "a"),))
    assert excinfo.value.field == "commands[start].description"


def test_command_description_minimum_is_three() -> None:
    with pytest.raises(ProfileValidationError):
        validate_commands((("start", "no"),))


def test_seeded_profile_passes_validation() -> None:
    validate_profile(DESCRIPTION, SHORT_DESCRIPTION, COMMANDS)


# --- Diffing ---------------------------------------------------------------


def test_diff_is_empty_when_live_matches_desired() -> None:
    assert compute_diff(_state(), _state()) == []


def test_diff_reports_each_differing_field_in_order() -> None:
    current = _state(
        description="old description",
        short_description="old short",
        commands=(("start", "Old welcome"),),
    )
    diffs = compute_diff(current, _state())
    assert [diff.name for diff in diffs] == [
        "description",
        "short_description",
        "commands",
    ]


def test_diff_reports_only_the_field_that_changed() -> None:
    current = _state(short_description="different short")
    diffs = compute_diff(current, _state())
    assert [diff.name for diff in diffs] == ["short_description"]
    assert diffs[0].old == "different short"
    assert diffs[0].new == "short"


def test_diff_detects_command_order_change() -> None:
    current = _state(commands=(("a", "text"), ("b", "text")))
    desired = _state(commands=(("b", "text"), ("a", "text")))
    assert [diff.name for diff in compute_diff(current, desired)] == ["commands"]


# --- Agreement with registered handlers -----------------------------------


def test_every_listed_command_has_a_registered_handler() -> None:
    missing, _ = compare_commands(COMMANDS, registered_command_names())
    assert missing == []


def test_hidden_commands_are_warnings_not_errors() -> None:
    _, unlisted = compare_commands(COMMANDS, registered_command_names())
    assert {"jobs", "folders", "retry"} <= set(unlisted)
    assert "start" not in unlisted
    assert "collect" not in unlisted


def test_compare_commands_reports_both_directions() -> None:
    missing, unlisted = compare_commands(
        (("listed_only", "text"), ("start", "text")),
        {"start", "hidden_only"},
    )
    assert missing == ["listed_only"]
    assert unlisted == ["hidden_only"]
