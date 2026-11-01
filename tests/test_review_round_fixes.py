"""Cold-email quality fixes: names that are page text, company names that are
SEO titles, a careers page that is really the homepage, a location read from
the company's own page, one contact per company, and Instantly's per-campaign
daily limit shared so the total stays inside max_daily_sends."""

import pytest
from bs4 import BeautifulSoup

from harvey.agents.scout import ROLE_WORD_RE, looks_like_brand, published_location
from harvey.agents.sender import casual_company
from harvey.collectors.people import find_people, split_person_name
from harvey.integrations.instantly import InstantlyClient
from tests.test_outbox_native import seed_prospect, state  # noqa: F401  (fixture)


# ── names ──

@pytest.mark.parametrize("text", ["Advance Directives", "Dallas Planning", "Firm Overview",
                                  "Wealth Manager", "Tax Attorney", "Grant Trust"])
def test_page_text_is_not_a_person(text):
    assert split_person_name(text) is None


@pytest.mark.parametrize("text", ["Mara Hollis", "Will Smith", "Dana Pryor", "Theo Brightwater"])
def test_real_names_still_pass(text):
    assert split_person_name(text) is not None


def test_the_firms_own_name_is_not_a_person_but_its_namesake_is():
    page = ("<div class='team'><h3>Pryor Wynn</h3><p>Partner</p>"
            "<h3>Dana Pryor</h3><p>Partner</p></div>")
    people = find_people([("https://pryorwynn.example/", page)], "pryorwynn.example")
    names = {(p["first_name"], p["last_name"]) for p in people}
    assert ("Pryor", "Wynn") not in names


# ── company names ──

@pytest.mark.parametrize("name, short", [
    ("Hollis & Kerr, PLLC", "Hollis & Kerr"),
    ("Brightwater Law PLC", "Brightwater Law"),
    ("Quillmont LLP", "Quillmont"),
    ("Harbor Oak", "Harbor Oak"),
    ("Fernbrook Insurance & Associates", "Fernbrook Insurance & Associates"),
    ("", ""),
])
def test_company_names_read_as_a_person_writes_them(name, short):
    assert casual_company(name) == short


@pytest.mark.parametrize("og, ok", [("Quillmont LLP", True), ("hollisandkerr", False),
                                    ("Home", False), ("example.com", False), ("", False)])
def test_og_site_name_counts_only_when_it_is_a_brand(og, ok):
    assert looks_like_brand(og) is ok


# ── location from the company's own page ──

@pytest.mark.parametrize("html, where", [
    ("<p>123 Main St, Suite 200 Dallas, TX 75201</p>", "Dallas, TX"),
    ('<script type="application/ld+json">{"address": {"@type": "PostalAddress", '
     '"addressLocality": "London", "addressCountry": "GB"}}</script>', "London, GB"),
    ('<script type="application/ld+json">{"@graph": [{"address": {"addressLocality": "Austin", '
     '"addressRegion": "TX", "addressCountry": "US"}}]}</script>', "Austin, TX"),
    ("<p>We serve clients worldwide.</p>", ""),
    ("<p>Call 404, ZZ 30328</p>", ""),
])
def test_published_location(html, where):
    assert published_location(BeautifulSoup(html, "html.parser")) == where


# ── hiring ──

def test_a_practice_area_is_not_a_job_title():
    assert not ROLE_WORD_RE.search("Estate Planning")
    assert ROLE_WORD_RE.search("Estate Planning Paralegal")


# ── one contact per company ──

@pytest.mark.asyncio
async def test_a_colleague_mid_sequence_holds_the_next_contact(state):  # noqa: F811
    from harvey.models.company import Company
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    first = await seed_prospect(state, email="ann@acme.example", status="contacted")
    second = await seed_prospect(state, email="bo@acme.example")
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET company_id = ?", (cid,))
        await db.commit()
    assert await state.colleague_in_sequence(second, 21)
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET updated_at = '2020-01-01T00:00:00' WHERE id = ?",
                         (first,))
        await db.commit()
    assert not await state.colleague_in_sequence(second, 21)


# ── Instantly ──

def _client(responses):
    client = InstantlyClient.__new__(InstantlyClient)
    client.calls = []

    async def fake_request(method, path, body=None, params=None):
        client.calls.append((method, path, body, params))
        return responses.get((method, path), {})
    client._request = fake_request
    return client


@pytest.mark.asyncio
async def test_delete_lead_sends_a_body_and_only_deletes_that_address():
    client = _client({("POST", "/leads/list"): {"items": [
        {"id": "1", "email": "jo@acme.example"}, {"id": "2", "email": "jo@acme.example.org"}]}})
    assert await client.delete_lead("JO@acme.example") == 1
    [delete] = [c for c in client.calls if c[0] == "DELETE"]
    assert delete[1] == "/leads/1" and delete[2] == {}


@pytest.mark.asyncio
async def test_the_daily_limit_is_shared_across_live_campaigns(state):  # noqa: F811
    from harvey.agents.sender import Sender
    from harvey.models.campaign import Campaign, EmailStep
    from tests.test_outbox_native import Cfg, Env
    cfg = Cfg()
    cfg.channels.email.max_daily_sends = 50
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    for i in range(5):
        await state.add_campaign(Campaign(
            id="", name=f"c{i}", channel="email", status="active", instantly_campaign_id=f"i{i}",
            sequence=[EmailStep(step=1, subject="s", body="b", delay_days=0)], prospect_ids=[]))
    live = [{"id": f"i{i}", "status": 1} for i in range(4)] + [{"id": "i4", "status": 3}]
    set_to = {}

    async def list_campaigns(limit=100):
        return live

    async def set_daily_limit(campaign_id, n):
        set_to[campaign_id] = n
        return {}
    sender.instantly.list_campaigns = list_campaigns
    sender.instantly.set_daily_limit = set_daily_limit
    assert await sender._share_daily_limit(extra=1) == 10   # 4 live + the new one
    assert set_to == {f"i{i}": 10 for i in range(4)}        # the finished one is left alone
    set_to.clear()
    await sender._share_daily_limit(extra=1)
    assert set_to == {}                                     # unchanged: not patched again


def test_native_sends_fill_instantlys_sender_tag_with_the_persona():
    from types import SimpleNamespace
    from harvey.agents.sender import Sender
    from harvey.models.prospect import Prospect
    sender = Sender.__new__(Sender)
    sender.config = SimpleNamespace(persona=SimpleNamespace(name="Pat Example"))
    p = Prospect(first_name="Jo", last_name="Doe", company="Acme, LLC", title="Owner",
                 email="jo@acme.example")
    assert sender._render("Hi {{first_name}} at {{company}}\n\n{{sendingAccountName}}", p) == \
        "Hi Jo at Acme\n\nPat Example"
