"""Round-tripping operator notes out of a vault file."""

from __future__ import annotations

from nemonis_vault.notes import extract_user_fields


class TestEmptyFieldsDoNotSwallowTheNextLabel:
    """The extraction regex used ``\\s*`` after each label, which consumes the
    newline separating it from the next. An *empty* field therefore captured the
    following label as its own value: an untouched note reported
    what_worked="**What failed:**", and that text would have travelled back
    toward the database as if the operator had written it.
    """

    def test_an_untouched_note_yields_no_fields(self) -> None:
        note = "## Review\n\n**What worked:**\n**What failed:**\n**Lesson:**\n"
        assert extract_user_fields(note) == {}

    def test_an_empty_field_does_not_capture_the_next_label(self) -> None:
        note = "**What worked:**\n**What failed:** Held through the news release\n"
        fields = extract_user_fields(note)
        assert "what_worked" not in fields
        assert fields["what_failed"] == "Held through the news release"

    def test_a_written_field_is_captured(self) -> None:
        note = "**What worked:** Waited for the pullback\n**What failed:**\n"
        assert extract_user_fields(note) == {"what_worked": "Waited for the pullback"}

    def test_all_three_fields_round_trip(self) -> None:
        note = (
            "**What worked:** Entry timing\n"
            "**What failed:** Exit was early\n"
            "**Lesson:** Let winners run to target\n"
        )
        assert extract_user_fields(note) == {
            "what_worked": "Entry timing",
            "what_failed": "Exit was early",
            "lesson": "Let winners run to target",
        }
