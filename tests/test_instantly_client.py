"""The Instantly client speaks Instantly's API v2 as it is documented."""

import pytest

from harvey.integrations.instantly import (
    InstantlyClient, instantly_steps, instantly_timezone, text_to_html,
)


def fake_client(responses=None):
    """A client whose requests are recorded; ``responses`` maps path -> reply."""
    client = InstantlyClient.__new__(InstantlyClient)
    client.calls = []
    responses = responses or {}

    async def fake_request(method, path, body=None, params=None):
        client.calls.append((method, path, body, params))
        return responses.get(path, {"id": "c1"})
    client._request = fake_request
    return client


@pytest.mark.asyncio
async def test_a_new_campaign_has_a_schedule_and_every_active_mailbox():
    client = fake_client({"/accounts": {"items": [
        {"email": "one@example.com"}, {"email": "two@example.com"}]}})
    await client.create_campaign("Draft", timezone="America/New_York", daily_limit=40)
    method, path, body, _ = client.calls[-1]
    assert (method, path) == ("POST", "/campaigns")
    [schedule] = body["campaign_schedule"]["schedules"]
    assert schedule["timezone"] == "America/Detroit"
    assert schedule["timing"] == {"from": "09:00", "to": "17:00"}
    assert set(schedule["days"]) == {"1", "2", "3", "4", "5"}
    assert body["email_list"] == ["one@example.com", "two@example.com"]
    assert body["daily_limit"] == 40
    assert body["text_only"] is True and body["open_tracking"] is False


@pytest.mark.asyncio
async def test_no_campaign_without_a_mailbox_to_send_from():
    client = fake_client({"/accounts": {"items": []}})
    assert await client.create_campaign("Draft") is None
    assert [c[1] for c in client.calls] == ["/accounts"]


def test_waits_move_to_the_step_before_and_tags_are_renamed():
    steps = instantly_steps([
        {"subject": "for {{first_name}}", "body": "Hi {{first_name}},\nAt {{company}}?", "wait": 0},
        {"subject": "two", "body": "Second", "wait": 3},
        {"subject": "three", "body": "Third", "wait": 4},
    ])
    assert [s["delay"] for s in steps] == [3, 4, 0]
    assert steps[0]["variants"][0] == {
        "subject": "for {{firstName}}",
        "body": "<div>Hi {{firstName}},</div><div>At {{company}}?</div>"}
    assert all(s["type"] == "email" and s["delay_unit"] == "days" for s in steps)


@pytest.mark.asyncio
async def test_the_sequence_is_patched_onto_the_campaign():
    client = fake_client()
    await client.set_campaign_emails("c1", [{"subject": "a", "body": "b", "wait": 0}])
    method, path, body, _ = client.calls[-1]
    assert (method, path) == ("PATCH", "/campaigns/c1")
    assert body["sequences"][0]["steps"][0]["variants"][0]["subject"] == "a"


def test_plain_text_becomes_a_div_per_line():
    # Instantly drops text that isn't inside an element.
    assert text_to_html("a < b & c\n\nd") == (
        "<div>a &lt; b &amp; c</div><div><br /></div><div>d</div>")


def test_zones_instantly_lacks_are_matched():
    assert instantly_timezone("America/Chicago") == "America/Chicago"
    assert instantly_timezone("America/Los_Angeles") == "America/Dawson"
    assert instantly_timezone("America/Phoenix") == "America/Creston"
    assert instantly_timezone("Not/AZone") == "America/Chicago"


@pytest.mark.asyncio
async def test_leads_go_to_the_bulk_endpoint_and_skip_known_contacts():
    client = fake_client()
    await client.add_leads("c1", [{"email": "pat@example.com"}])
    method, path, body, _ = client.calls[-1]
    assert (method, path) == ("POST", "/leads/add")
    assert body["campaign_id"] == "c1" and body["skip_if_in_workspace"] is True


@pytest.mark.asyncio
async def test_replies_are_read_from_received_emails():
    client = fake_client({"/emails": {"items": [{
        "id": "e1", "lead": "pat@example.com", "subject": "Re: a note",
        "body": {"text": "Tell me more", "html": "<p>Tell me more</p>"}}]}})
    [reply] = await client.get_replies("c1")
    assert reply == {"uuid": "e1", "lead_email": "pat@example.com",
                     "body": "Tell me more", "subject": "Re: a note"}
    _, path, _, params = client.calls[-1]
    assert path == "/emails" and params["email_type"] == "received"
    assert params["campaign_id"] == "c1"


@pytest.mark.asyncio
async def test_a_reply_goes_out_from_the_mailbox_that_was_written_to():
    client = fake_client({"/emails/e1": {"eaccount": "one@example.com", "subject": "a note"}})
    await client.send_reply("e1", "Happy to help.\nPat")
    method, path, body, _ = client.calls[-1]
    assert (method, path) == ("POST", "/emails/reply")
    assert body["eaccount"] == "one@example.com"
    assert body["subject"] == "Re: a note"
    assert body["body"] == {"text": "Happy to help.\nPat", "html": "<div>Happy to help.</div><div>Pat</div>"}
