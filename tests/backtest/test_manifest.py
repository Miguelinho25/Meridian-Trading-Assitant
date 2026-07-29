"""The reproducibility manifest.

The failure this guards against is a manifest that *looks* complete: it records
a commit, a seed and a provider, and gives every appearance of pinning the run
while silently omitting something that changes the result. Such a manifest is
worse than none, because two incomparable runs would compare equal.
"""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from nemonis_backtest.manifest import (
    BacktestManifest,
    CodeIdentity,
    DataIdentity,
    ExecutionModel,
    ModelIdentity,
    canonical_hash,
    capture_code_identity,
    dataset_fingerprint,
    result_hash,
)

T0 = datetime(2020, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 1, tzinfo=UTC)


def a_manifest(**overrides) -> BacktestManifest:
    base = {
        "strategy_key": "ema-pullback",
        "strategy_version": "1.2.0",
        "parameters": {"fast": 20, "slow": 50},
        "seed": 42,
        "code": CodeIdentity(
            git_commit="a" * 40,
            git_branch="main",
            dirty=False,
            engine_version="0.1.0",
            feature_pipeline_version="1.0.0",
            risk_profile_version="risk-profiles@0.1.0",
        ),
        "data": DataIdentity(
            provider="yahoo",
            dataset_version="2026-07-01",
            instruments=("EURUSD", "GBPUSD"),
            timeframe="D1",
            start=T0,
            end=T1,
            provenance="REAL",
            spread_assumed=True,
            bar_count=4000,
        ),
        "execution": ExecutionModel(
            slippage_model="PROPORTIONAL_TO_SPREAD",
            fixed_slippage_pips=Decimal("0.5"),
            spread_fraction=Decimal("0.25"),
            gap_penalty=Decimal("2.0"),
            commission_model="PER_LOT",
            commission_per_lot=Decimal("7.00"),
            spread_model="ASSUMED_FROM_MID",
            starting_balance=Decimal("100000"),
            account_currency="USD",
            risk_profile="CHALLENGE",
            warmup_bars=51,
        ),
        "models": ModelIdentity(),
    }
    return BacktestManifest(**{**base, **overrides})


class TestHashStability:
    def test_identical_manifests_hash_identically(self) -> None:
        assert a_manifest().manifest_hash == a_manifest().manifest_hash

    def test_the_hash_is_stable_across_dict_ordering(self) -> None:
        """Parameters written in a different order are the same experiment."""
        a = a_manifest(parameters={"fast": 20, "slow": 50})
        b = a_manifest(parameters={"slow": 50, "fast": 20})
        assert a.manifest_hash == b.manifest_hash

    def test_decimals_hash_as_text_not_floats(self) -> None:
        """float(Decimal) varies in its last bits by platform; a manifest hash
        that differs by machine makes every cross-machine comparison a false
        mismatch."""
        assert canonical_hash({"v": Decimal("0.1")}) == canonical_hash({"v": Decimal("0.1")})
        assert canonical_hash({"v": Decimal("0.10")}) != canonical_hash({"v": Decimal("0.1")})


class TestEveryInputAffectsTheHash:
    """A field that does not change the hash is silently excluded from
    reproducibility: two runs differing in it would compare as identical."""

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("strategy_key", "other-strategy"),
            ("strategy_version", "1.2.1"),
            ("parameters", {"fast": 21, "slow": 50}),
            ("seed", 43),
        ],
    )
    def test_top_level_fields_are_in_the_hash(self, field_name: str, value: object) -> None:
        assert a_manifest(**{field_name: value}).manifest_hash != a_manifest().manifest_hash

    @pytest.mark.parametrize(
        "field_name",
        [
            "git_commit",
            "git_branch",
            "dirty",
            "engine_version",
            "feature_pipeline_version",
            "risk_profile_version",
        ],
    )
    def test_every_code_identity_field_is_in_the_hash(self, field_name: str) -> None:
        base = a_manifest()
        current = getattr(base.code, field_name)
        mutated = not current if isinstance(current, bool) else f"{current}-changed"
        assert (
            a_manifest(code=dataclasses.replace(base.code, **{field_name: mutated})).manifest_hash
            != base.manifest_hash
        )

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("provider", "dukascopy"),
            ("dataset_version", "2026-08-01"),
            ("instruments", ("EURUSD",)),
            ("timeframe", "H1"),
            ("start", datetime(2021, 1, 1, tzinfo=UTC)),
            ("end", datetime(2025, 1, 1, tzinfo=UTC)),
            ("provenance", "SYNTHETIC"),
            ("spread_assumed", False),
            ("bar_count", 3999),
        ],
    )
    def test_every_data_identity_field_is_in_the_hash(self, field_name: str, value: object) -> None:
        base = a_manifest()
        assert (
            a_manifest(data=dataclasses.replace(base.data, **{field_name: value})).manifest_hash
            != base.manifest_hash
        )

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("slippage_model", "FIXED"),
            ("fixed_slippage_pips", Decimal("1.0")),
            # The parameter, not just the model name: recording "proportional"
            # without its fraction lets two disagreeing runs share a hash.
            ("spread_fraction", Decimal("0.50")),
            ("gap_penalty", Decimal("3.0")),
            ("commission_model", "NONE"),
            ("commission_per_lot", Decimal("3.50")),
            ("spread_model", "REAL_BID_ASK"),
            ("starting_balance", Decimal("50000")),
            ("account_currency", "GBP"),
            ("risk_profile", "PRESERVATION"),
            ("warmup_bars", 100),
        ],
    )
    def test_every_execution_model_field_is_in_the_hash(
        self, field_name: str, value: object
    ) -> None:
        base = a_manifest()
        assert (
            a_manifest(
                execution=dataclasses.replace(base.execution, **{field_name: value})
            ).manifest_hash
            != base.manifest_hash
        )

    def test_model_identity_is_in_the_hash(self) -> None:
        base = a_manifest()
        assert (
            a_manifest(models=ModelIdentity(models={"critique": "llama3.2:3b@v1"})).manifest_hash
            != base.manifest_hash
        )

    def test_no_manifest_field_is_omitted_from_the_hash(self) -> None:
        """Catches a field added later without being wired into the hash."""
        hashed = set(a_manifest().canonical())
        declared = {f.name for f in dataclasses.fields(BacktestManifest)}
        assert declared == hashed, f"not hashed: {declared - hashed}"


class TestDirtyTreesAreNotReproducible:
    """A commit hash does not identify the code if the tree was dirty. A manifest
    that records the commit and stops looks reproducible while being nothing of
    the sort."""

    def test_a_clean_tree_is_reproducible(self) -> None:
        assert a_manifest().is_reproducible
        assert a_manifest().irreproducible_reason == ""

    def test_a_dirty_tree_is_not_reproducible(self) -> None:
        base = a_manifest()
        dirty = a_manifest(code=dataclasses.replace(base.code, dirty=True))
        assert not dirty.is_reproducible
        assert "does not identify the code" in dirty.irreproducible_reason

    def test_a_missing_commit_is_not_reproducible(self) -> None:
        base = a_manifest()
        nogit = a_manifest(code=dataclasses.replace(base.code, git_commit=""))
        assert not nogit.is_reproducible
        assert "unidentified" in nogit.irreproducible_reason

    def test_dirty_state_changes_the_hash(self) -> None:
        """Two runs at the same commit, one dirty, are not the same experiment."""
        base = a_manifest()
        dirty = a_manifest(code=dataclasses.replace(base.code, dirty=True))
        assert dirty.manifest_hash != base.manifest_hash


class TestCaptureCodeIdentity:
    def test_a_clean_repo_reports_not_dirty(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("one")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

        identity = capture_code_identity(
            tmp_path,
            engine_version="0.1.0",
            feature_pipeline_version="1.0.0",
            risk_profile_version="risk-profiles@0.1.0",
        )
        assert identity.git_commit
        assert not identity.dirty
        assert identity.is_pinned

    def test_a_modified_tracked_file_reports_dirty(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("one")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("two")

        identity = capture_code_identity(
            tmp_path,
            engine_version="0.1.0",
            feature_pipeline_version="1.0.0",
            risk_profile_version="risk-profiles@0.1.0",
        )
        assert identity.dirty
        assert not identity.is_pinned

    def test_an_untracked_file_does_not_count_as_dirty(self, tmp_path: Path) -> None:
        """A scratch file in the working directory does not change what ran."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("one")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
        (tmp_path / "scratch.log").write_text("noise")

        assert not capture_code_identity(
            tmp_path,
            engine_version="0.1.0",
            feature_pipeline_version="1.0.0",
            risk_profile_version="risk-profiles@0.1.0",
        ).dirty

    def test_outside_a_repo_is_unpinned_rather_than_an_error(self, tmp_path: Path) -> None:
        """A run outside a checkout is still a run; it is recorded as unpinned."""
        identity = capture_code_identity(
            tmp_path,
            engine_version="0.1.0",
            feature_pipeline_version="1.0.0",
            risk_profile_version="risk-profiles@0.1.0",
        )
        assert not identity.is_pinned


class TestResultHash:
    def test_identical_results_hash_identically(self) -> None:
        args = {
            "metrics": {"net_pnl": Decimal("100")},
            "trade_count": 2,
            "final_balance": Decimal("100100"),
            "trade_fingerprints": ["a", "b"],
        }
        assert result_hash(**args) == result_hash(**args)

    def test_different_trades_with_the_same_summary_are_distinguished(self) -> None:
        """Two runs can reach the same net P&L through different trades. The
        summary alone would hide that determinism had broken."""
        common = {
            "metrics": {"net_pnl": Decimal("100")},
            "trade_count": 2,
            "final_balance": Decimal("100100"),
        }
        assert result_hash(**common, trade_fingerprints=["a", "b"]) != result_hash(
            **common, trade_fingerprints=["c", "d"]
        )

    def test_trade_order_is_significant(self) -> None:
        """Same trades in a different sequence is a different execution path."""
        common = {
            "metrics": {},
            "trade_count": 2,
            "final_balance": Decimal("0"),
        }
        assert result_hash(**common, trade_fingerprints=["a", "b"]) != result_hash(
            **common, trade_fingerprints=["b", "a"]
        )


class TestDatasetFingerprint:
    """The dataset must be identified by content, not by fetch time.

    Using the run date broke in both directions: identical data on two days
    looked like two experiments, and — the dangerous one — *changed* data on the
    same day looked like one. Divergent results would then be reported as a
    determinism break when the data underneath had simply moved.
    """

    def _bar(self, when: datetime, close: str) -> object:
        from types import SimpleNamespace

        c = Decimal(close)
        return SimpleNamespace(
            open_time=when,
            bid_open=c,
            bid_high=c,
            bid_low=c,
            bid_close=c,
            ask_open=c,
            ask_high=c,
            ask_low=c,
            ask_close=c,
        )

    def _series(self, closes: list[str], instrument: str = "EURUSD") -> dict:
        return {
            instrument: [
                self._bar(datetime(2020, 1, i + 1, tzinfo=UTC), c) for i, c in enumerate(closes)
            ]
        }

    def test_identical_data_fingerprints_identically(self) -> None:
        a = self._series(["1.1000", "1.1010"])
        b = self._series(["1.1000", "1.1010"])
        assert dataset_fingerprint(a) == dataset_fingerprint(b)

    def test_a_changed_price_changes_the_fingerprint(self) -> None:
        """The case the run-date version could not see."""
        a = self._series(["1.1000", "1.1010"])
        b = self._series(["1.1000", "1.1011"])
        assert dataset_fingerprint(a) != dataset_fingerprint(b)

    def test_a_truncated_series_does_not_collide(self) -> None:
        a = self._series(["1.1000", "1.1010", "1.1020"])
        b = self._series(["1.1000", "1.1010"])
        assert dataset_fingerprint(a) != dataset_fingerprint(b)

    def test_a_different_instrument_changes_the_fingerprint(self) -> None:
        a = self._series(["1.1000"], instrument="EURUSD")
        b = self._series(["1.1000"], instrument="GBPUSD")
        assert dataset_fingerprint(a) != dataset_fingerprint(b)

    def test_load_order_does_not_matter(self) -> None:
        """Instruments arriving in a different order are the same dataset."""
        one = {**self._series(["1.1"], "EURUSD"), **self._series(["1.3"], "GBPUSD")}
        two = {**self._series(["1.3"], "GBPUSD"), **self._series(["1.1"], "EURUSD")}
        assert dataset_fingerprint(one) == dataset_fingerprint(two)

    def test_the_fingerprint_flows_into_the_manifest_hash(self) -> None:
        """Two runs over different data must not share a manifest hash."""
        base = a_manifest()
        moved = a_manifest(data=dataclasses.replace(base.data, dataset_version="sha256:different"))
        assert moved.manifest_hash != base.manifest_hash
