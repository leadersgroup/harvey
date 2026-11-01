"""`compliance.allowed_countries`: cold email only to prospects Harvey is sure
are in one of those countries. Elsewhere is never emailed; unknown waits for you."""

import pytest
from pydantic import ValidationError

from harvey.config import ComplianceConfig
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)

BODY = "A short, plain note. Question?\n\n123 Example St, Springfield"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _cold(state, approved_by="autopilot"):
    pid = await seed_prospect(state, email="jane@acme.example")
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="sequence",
        approved_by=approved_by,
    )


def _sender(state, provider, country):
    sender = make_sender(state, provider, require_approval=False)
    sender.config.compliance = ComplianceConfig(
        postal_address="123 Example St, Springfield", allowed_countries=["US"])

    async def recipient_country(prospect):
        return country
    sender._recipient_country = recipient_country
    return sender


def test_codes_are_normalised():
    assert ComplianceConfig(allowed_countries=["usa", "us", "UK"]).allowed_countries == ["US", "GB"]
    assert ComplianceConfig(allowed_countries="US").allowed_countries == ["US"]
    assert ComplianceConfig().allowed_countries == []


@pytest.mark.parametrize("bad", [["United States"], [True], 5])
def test_a_bad_code_is_a_config_error(bad):
    with pytest.raises(ValidationError):
        ComplianceConfig(allowed_countries=bad)


@pytest.mark.asyncio
async def test_a_prospect_in_an_allowed_country_is_emailed(state):
    await _cold(state)
    provider = FakeProvider()
    await _sender(state, provider, "US")._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_a_prospect_elsewhere_is_never_emailed_even_when_approved(state):
    item_id = await _cold(state, approved_by="you")
    provider = FakeProvider()
    await _sender(state, provider, "CA")._drain_due()
    assert provider.sent == []
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "cancelled" and "CA" in item["error"]


@pytest.mark.asyncio
async def test_an_unknown_country_waits_for_you(state):
    item_id = await _cold(state)
    provider = FakeProvider()
    await _sender(state, provider, "")._drain_due()
    assert provider.sent == []
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "pending_review" and "country not known" in item["error"]


@pytest.mark.asyncio
async def test_an_unknown_country_you_approved_is_sent(state):
    await _cold(state, approved_by="you")
    provider = FakeProvider()
    await _sender(state, provider, "")._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_no_list_sends_anywhere(state):
    await _cold(state)
    provider = FakeProvider()
    sender = _sender(state, provider, "CA")
    sender.config.compliance = ComplianceConfig(postal_address="123 Example St, Springfield")
    await sender._drain_due()
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_instantly_upload_leaves_out_unknown_and_foreign_prospects(state):
    from harvey.agents.sender import Sender
    from tests.test_outbox_native import Cfg, Env
    cfg = Cfg()
    cfg.compliance = ComplianceConfig(allowed_countries=["US"])
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    countries = {"us@a.example": "US", "ca@b.example": "CA", "unknown@c.example": ""}

    async def recipient_country(prospect):
        return countries[prospect.email]
    sender._recipient_country = recipient_country
    results = {}
    for email in countries:
        p = await state.get_prospect(await seed_prospect(state, email=email))
        results[email] = await sender._outside_countries(p)
    assert results["us@a.example"] is None
    assert results["ca@b.example"][0] is True
    assert results["unknown@c.example"][0] is False
