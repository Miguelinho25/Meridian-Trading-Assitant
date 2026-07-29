"""Prop-firm rule profiles.

The headline percentages are not the risk. Whether an account survives is
decided by how the limits are *measured*, and by whether anyone has checked the
rules against the firm's actual terms.
"""

from __future__ import annotations

from httpx import AsyncClient


class TestUnverifiedRulesAreProminent:
    """An unverified rule set is more dangerous than none: precise-looking
    numbers invite a confidence they have not earned."""

    async def test_the_bundled_profile_is_not_verified(self, client: AsyncClient) -> None:
        for p in (await client.get("/api/prop-firm")).json():
            assert p["verification"]["is_verified"] is False

    async def test_an_unverified_profile_carries_a_warning(self, client: AsyncClient) -> None:
        for p in (await client.get("/api/prop-firm")).json():
            warning = p["verification"]["warning"]
            assert warning, f"{p['profile_id']} is unverified and says nothing about it"
            assert "never been verified" in warning

    async def test_the_name_itself_disclaims_being_a_real_firm(self, client: AsyncClient) -> None:
        """Someone will screenshot the card without the warning."""
        for p in (await client.get("/api/prop-firm")).json():
            assert "NOT A REAL FIRM" in p["name"].upper()

    async def test_the_source_is_stated(self, client: AsyncClient) -> None:
        for p in (await client.get("/api/prop-firm")).json():
            assert p["verification"]["source"]


class TestMeasurementRulesCarryTheirConsequence:
    """A UI showing "TRAILING" alone tells an operator nothing actionable."""

    async def test_the_three_definitional_choices_are_returned(self, client: AsyncClient) -> None:
        for p in (await client.get("/api/prop-firm")).json():
            fields = {d["field_name"] for d in p["definitions"]}
            assert fields == {
                "daily_loss_basis",
                "daily_loss_reference",
                "total_loss_type",
            }

    async def test_every_choice_explains_what_it_means(self, client: AsyncClient) -> None:
        for p in (await client.get("/api/prop-firm")).json():
            for d in p["definitions"]:
                assert len(d["consequence"]) > 40, d["field_name"]

    async def test_equity_basis_warns_about_floating_losses(self, client: AsyncClient) -> None:
        """The trap: an open position under water can breach the daily limit
        without a single trade being closed."""
        for p in (await client.get("/api/prop-firm")).json():
            basis = next(d for d in p["definitions"] if d["field_name"] == "daily_loss_basis")
            if basis["value"] == "EQUITY":
                assert "floating" in basis["consequence"].lower()
                assert basis["stricter_option"] is True

    async def test_the_stricter_option_of_each_pair_is_flagged(self, client: AsyncClient) -> None:
        stricter = {
            "daily_loss_basis": "EQUITY",
            "daily_loss_reference": "HIGHEST_EQUITY",
            "total_loss_type": "TRAILING",
        }
        for p in (await client.get("/api/prop-firm")).json():
            for d in p["definitions"]:
                assert d["stricter_option"] == (d["value"] == stricter[d["field_name"]])


class TestReadOnly:
    async def test_rules_cannot_be_edited_over_http(self, client: AsyncClient) -> None:
        """Rules come from the firm. A route that let them be edited here would
        let an operator relax an evaluation limit they do not control."""
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/prop-firm")).status_code == 405
