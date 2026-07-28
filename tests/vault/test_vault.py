"""Vault safety. A Markdown edit must never be able to move money."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from meridian_broker.broker import ClosedTrade
from meridian_broker.fills import FillReason
from meridian_marketdata.instruments import get_spec
from meridian_schemas.enums import Direction
from meridian_vault.notes import (
    TRADE_EDITABLE_FIELDS,
    detect_conflicts,
    extract_user_fields,
    hash_content,
    parse_note,
    render_trade_note,
    trade_note_filename,
)
from meridian_vault.writer import VaultError, VaultWriter, slugify

T = datetime(2026, 7, 27, 14, 30, tzinfo=UTC)
EURUSD = get_spec("EURUSD")


@pytest.fixture
def vault(tmp_path) -> VaultWriter:
    return VaultWriter(tmp_path / "vault")


def a_trade(**kw) -> ClosedTrade:
    base = {
        "trade_id": "tr_01JQ8X4M2NABCDEF",
        "instrument": "EURUSD",
        "direction": Direction.LONG,
        "lots": Decimal("0.35"),
        "entry_price": Decimal("1.08432"),
        "exit_price": Decimal("1.08192"),
        "opened_at": T,
        "closed_at": T,
        "strategy_id": "ma-trend",
        "pnl_account_ccy": Decimal("-350.00"),
        "commission": Decimal("1.23"),
        "reason": FillReason.STOP_LOSS,
        "mfe_pips": Decimal("6.2"),
        "mae_pips": Decimal("24.0"),
    }
    return ClosedTrade(**{**base, **kw})


class TestPathTraversalIsRefused:
    """Note names derive partly from user text, so a filename is untrusted."""

    @pytest.mark.parametrize(
        "attempt",
        [
            "../escape.md",
            "../../etc/passwd",
            "sub/../../outside.md",
            "/etc/passwd",
        ],
    )
    def test_escape_attempts_are_refused(self, vault, attempt) -> None:
        with pytest.raises(VaultError, match="outside the vault"):
            vault.write(attempt, "content")

    def test_a_legitimate_subfolder_is_allowed(self, vault) -> None:
        result = vault.write("note.md", "content", folder="01-Trades")
        assert result.written
        assert result.path.parent.name == "01-Trades"

    def test_a_symlink_pointing_outside_is_refused(self, vault, tmp_path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (vault.root / "escape").symlink_to(outside)
        with pytest.raises(VaultError, match="outside the vault"):
            vault.write("escape/note.md", "content")


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected_absent"),
        [("a/b", "/"), ("a\\b", "\\"), ("a:b", ":"), ("a\x00b", "\x00")],
    )
    def test_separators_and_control_characters_are_stripped(self, raw, expected_absent) -> None:
        assert expected_absent not in slugify(raw)

    def test_reserved_windows_names_are_suffixed(self) -> None:
        """A vault synced to Windows would silently fail to create these."""
        assert slugify("CON") != "CON"
        assert slugify("aux").startswith("aux-")

    def test_empty_input_becomes_untitled(self) -> None:
        assert slugify("   ") == "untitled"
        assert slugify("///") == "untitled"

    def test_length_is_capped(self) -> None:
        assert len(slugify("x" * 500)) <= 120

    def test_unicode_is_normalised(self) -> None:
        assert slugify("café-trade") == "cafe-trade"


class TestAtomicWrites:
    def test_a_new_note_is_written(self, vault) -> None:
        result = vault.write("a.md", "hello")
        assert result.written
        assert result.path.read_text() == "hello"

    def test_unchanged_content_is_a_no_op(self, vault) -> None:
        """Otherwise a sync loop churns the vault and mtimes stop meaning anything."""
        vault.write("a.md", "hello")
        result = vault.write("a.md", "hello")
        assert not result.written
        assert result.reason == "unchanged"

    def test_changed_content_is_backed_up_first(self, vault) -> None:
        vault.write("a.md", "original")
        result = vault.write("a.md", "revised", at=T)
        assert result.written
        assert result.backup is not None
        assert result.backup.read_text() == "original"
        assert result.path.read_text() == "revised"

    def test_no_temp_file_is_left_behind(self, vault) -> None:
        vault.write("a.md", "content")
        assert not list(vault.root.glob(".*.tmp"))

    def test_backups_are_pruned(self, vault) -> None:
        """Unbounded backup growth is its own bug."""
        writer = VaultWriter(vault.root, backup_retention=3)
        for i in range(8):
            writer.write("a.md", f"version {i}", at=datetime(2026, 7, 27, 10, i, tzinfo=UTC))
        assert len(list(vault.root.glob("a.md.backup-*"))) <= 3

    def test_backups_are_excluded_from_note_listings(self, vault) -> None:
        vault.write("a.md", "one")
        vault.write("a.md", "two", at=T)
        assert [p.name for p in vault.list_notes()] == ["a.md"]


class TestTradeNotes:
    def test_frontmatter_is_machine_readable(self) -> None:
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        parsed = parse_note(note)
        assert parsed.frontmatter["meridian_type"] == "trade"
        assert parsed.frontmatter["instrument"] == "EURUSD"
        assert parsed.frontmatter["meridian_schema"] == "trade-note@1"

    def test_the_editable_allowlist_is_published_in_the_note(self) -> None:
        """Visible to the reader and to the sync engine alike."""
        parsed = parse_note(render_trade_note(a_trade(), spec=EURUSD, generated_at=T))
        assert set(parsed.frontmatter["meridian_editable"]) == set(TRADE_EDITABLE_FIELDS)

    def test_synthetic_flag_propagates(self) -> None:
        """So simulated performance is never mistaken for real in any view."""
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T, synthetic=True)
        assert parse_note(note).frontmatter["synthetic"] == "true"

    def test_wiki_links_come_from_the_controlled_vocabulary(self) -> None:
        note = render_trade_note(
            a_trade(), spec=EURUSD, generated_at=T, regime_label="TRENDING/HIGH"
        )
        assert "[[EURUSD]]" in note
        assert "[[Trend Regime]]" in note
        assert "[[High Volatility]]" in note
        assert "[[Strategy-ma-trend]]" in note

    def test_an_ambiguous_exit_is_disclosed(self) -> None:
        note = render_trade_note(a_trade(ambiguous_exit=True), spec=EURUSD, generated_at=T)
        assert "Ambiguous exit" in note

    def test_filename_is_safe_and_unique(self) -> None:
        name = trade_note_filename(a_trade())
        assert name.endswith(".md")
        assert "/" not in name
        assert "2026-07-27" in name

    def test_generation_is_deterministic(self) -> None:
        a = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        b = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        assert a == b


class TestUserFieldsAreTheOnlyWayBack:
    def test_allowlisted_fields_are_extracted(self) -> None:
        note = render_trade_note(
            a_trade(),
            spec=EURUSD,
            generated_at=T,
            user_fields={"lesson": "Do not fade the London open."},
        )
        assert extract_user_fields(note)["lesson"] == "Do not fade the London open."

    def test_nothing_financial_is_extractable(self) -> None:
        """The sync-back path can only ever carry these keys."""
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        extracted = extract_user_fields(note)
        for forbidden in ("pnl", "balance", "equity", "lots", "entry", "exit"):
            assert forbidden not in extracted

    def test_an_edit_to_a_generated_field_is_detected(self) -> None:
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        original_hash = str(parse_note(note).frontmatter["meridian_content_hash"])

        # The stored value is quoted, because it contains a colon. Matching the
        # unquoted form made an earlier version of this test replace nothing and
        # then find no conflict — a mutation test must confirm its mutation landed.
        tampered = note.replace(original_hash, "sha256:0000deadbeef")
        assert tampered != note, "tamper did not apply; the test would prove nothing"
        assert parse_note(tampered).frontmatter["meridian_content_hash"] != original_hash

        assert detect_conflicts(tampered, original_hash)

    def test_an_edit_to_a_user_field_is_not_a_conflict(self) -> None:
        """Otherwise every legitimate review would trigger a false conflict."""
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        parsed = parse_note(note)
        edited = note.replace("**Lesson:** ", "**Lesson:** Wait for confirmation.")
        assert not detect_conflicts(edited, str(parsed.frontmatter["meridian_content_hash"]))


class TestParserTolerance:
    def test_a_note_without_frontmatter_still_parses(self) -> None:
        """Refusing to read a malformed note would lose the user's work."""
        parsed = parse_note("just some text")
        assert parsed.frontmatter == {}
        assert parsed.body == "just some text"

    def test_truncated_frontmatter_does_not_raise(self) -> None:
        assert parse_note("---\nkey: value\nno closing delimiter") is not None

    def test_lists_round_trip(self) -> None:
        note = render_trade_note(a_trade(), spec=EURUSD, generated_at=T)
        tags = parse_note(note).frontmatter["tags"]
        assert isinstance(tags, list)
        assert "trade" in tags


class TestContentHash:
    def test_identical_content_hashes_identically(self) -> None:
        assert hash_content("abc") == hash_content("abc")

    def test_different_content_differs(self) -> None:
        assert hash_content("abc") != hash_content("abd")
