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


def _cfg(**email):
    """A test config of its own: Cfg's settings are class attributes, so
    changing them on one instance would change them for every later test."""
    from types import SimpleNamespace
    from tests.test_outbox_native import Cfg
    base = {k: v for k, v in vars(Cfg.channels.email).items() if not k.startswith("_")}
    cfg = Cfg()
    cfg.channels = SimpleNamespace(email=SimpleNamespace(**{**base, **email}),
                                   linkedin=Cfg.channels.linkedin)
    return cfg


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
    from tests.test_outbox_native import Env
    sender = Sender(brain=None, state=state, config=_cfg(provider="instantly"), env=Env())
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


# ── Instantly: "Harvey - " names, and one live campaign per group ──

def test_instantly_names_start_with_harvey():
    from harvey.agents.sender import instantly_name
    assert instantly_name("Tax Law Firms-outreach") == "Harvey - Tax Law Firms-outreach"
    assert instantly_name("Harvey - Tax Law Firms-outreach") == "Harvey - Tax Law Firms-outreach"


async def _live_campaign(state, name, prospect_ids, instantly_id="i1"):
    from harvey.models.campaign import Campaign, EmailStep
    return await state.add_campaign(Campaign(
        id="", name=name, channel="email", status="active", instantly_campaign_id=instantly_id,
        sequence=[EmailStep(step=1, subject="s", body="Hi {{first_name}}", delay_days=0)],
        prospect_ids=prospect_ids))


@pytest.mark.asyncio
async def test_new_prospects_join_the_live_campaign_for_their_group(state):  # noqa: F811
    from harvey.agents.writer import Writer
    first = await seed_prospect(state, email="ann@one.example", status="contacted")
    cid = await _live_campaign(state, "Tax Law Firms-outreach", [first])
    newcomer = await state.get_prospect(await seed_prospect(state, email="bo@two.example"))
    writer = Writer(brain=None, state=state, config=_cfg(provider="instantly"))
    assert await writer._join_live_campaign("Tax Law Firms-outreach", [newcomer])
    [campaign] = await state.get_campaigns_by_status("active")
    assert campaign.id == cid and campaign.prospect_ids == [first, newcomer.id]
    assert (await state.get_prospect(newcomer.id)).status == "queued"
    assert not await writer._join_live_campaign("Elder Law Firms-outreach", [newcomer])


@pytest.mark.asyncio
async def test_a_top_up_uploads_only_the_newcomers_and_keeps_the_emails(state):  # noqa: F811
    from harvey.agents.sender import Sender
    from tests.test_outbox_native import Env
    cfg = _cfg(provider="instantly", require_approval=False)
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    sent = await seed_prospect(state, email="ann@one.example", status="contacted")
    new = await seed_prospect(state, email="bo@two.example", status="queued")
    await _live_campaign(state, "Tax Law Firms-outreach", [sent, new])
    calls = []

    async def get_campaign(campaign_id):
        return {"id": campaign_id, "status": 1}

    async def record(name):
        async def call(*args, **kwargs):
            calls.append((name, args))
            return {}
        return call
    sender.instantly.get_campaign = get_campaign
    for name in ("set_campaign_emails", "add_leads", "activate_campaign", "list_campaigns",
                 "set_daily_limit"):
        setattr(sender.instantly, name, await record(name))
    await sender.run()
    names = [c[0] for c in calls]
    assert "set_campaign_emails" not in names
    [(_, (campaign_id, leads))] = [c for c in calls if c[0] == "add_leads"]
    assert [lead["email"] for lead in leads] == ["bo@two.example"]
    assert (await state.get_prospect(new)).status == "contacted"


@pytest.mark.asyncio
async def test_a_campaign_you_paused_in_instantly_gets_no_new_leads(state):  # noqa: F811
    from harvey.agents.sender import Sender
    from tests.test_outbox_native import Env
    cfg = _cfg(provider="instantly", require_approval=False)
    sender = Sender(brain=None, state=state, config=cfg, env=Env())
    new = await seed_prospect(state, email="bo@two.example", status="queued")
    campaign_id = await _live_campaign(state, "Tax Law Firms-outreach", [new])
    added = []

    async def get_campaign(campaign_id):
        return {"id": campaign_id, "status": 2}

    async def add_leads(*args):
        added.append(args)
        return {}
    sender.instantly.get_campaign = get_campaign
    sender.instantly.add_leads = add_leads
    [campaign] = await state.get_campaigns_by_status("active")
    assert campaign.id == campaign_id
    await sender._deploy_campaign(campaign, top_up=True)
    assert added == [] and (await state.get_prospect(new)).status == "queued"


# ── email: a guess with no evidence that bounces gets two more tries ──

async def _find(monkeypatch, verdicts, source="default"):
    from harvey.integrations import email_finder as ef
    checked = []

    async def mx(domain):
        return "mx.example.net"

    async def pattern(domain, **kw):
        return "{f}.{l}", source, 0.3

    async def verify(email, mx_type, env):
        checked.append(email)
        return verdicts.get(email.split("@")[0]), False
    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "derive_pattern", pattern)
    monkeypatch.setattr(ef, "_verify_candidate", verify)
    result = await ef.find_email("Mara", "Hollis", "hollislaw.example")
    return result, checked


@pytest.mark.asyncio
async def test_a_bounced_guess_tries_first_then_flast(monkeypatch):
    result, checked = await _find(monkeypatch, {"mara.hollis": "invalid", "mara": "invalid",
                                               "mhollis": "verified"})
    assert checked == ["mara.hollis@hollislaw.example", "mara@hollislaw.example",
                       "mhollis@hollislaw.example"]
    assert (result.email, result.status) == ("mhollis@hollislaw.example", "verified")


@pytest.mark.asyncio
async def test_an_undecided_retry_stops_spending_credits(monkeypatch):
    result, checked = await _find(monkeypatch, {"mara.hollis": "invalid", "mara": None})
    assert len(checked) == 2 and result.status == "invalid"


@pytest.mark.asyncio
async def test_a_pattern_with_evidence_is_not_second_guessed(monkeypatch):
    result, checked = await _find(monkeypatch, {"mara.hollis": "invalid"}, source="site_address")
    assert checked == ["mara.hollis@hollislaw.example"] and result.status == "invalid"


def test_request_urls_with_api_keys_are_not_logged():
    import logging
    import harvey.main  # noqa: F401  (configures logging)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


@pytest.mark.asyncio
async def test_a_writer_with_everyone_held_back_says_so(state):  # noqa: F811
    """The heartbeat prospects instead when this comes back, rather than
    choosing to write every cycle with nothing it may write."""
    import aiosqlite
    from types import SimpleNamespace
    from harvey.agents.writer import NOTHING_TO_WRITE, Writer
    from harvey.models.company import Company
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await seed_prospect(state, email="ann@acme.example", status="contacted")
    await seed_prospect(state, email="bo@acme.example")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET company_id = ?", (cid,))
        await db.commit()
    brain = SimpleNamespace(load_skills_for_agent=lambda agent: "")
    writer = Writer(brain=brain, state=state, config=_cfg(provider="instantly"))
    assert await writer.run() == NOTHING_TO_WRITE


# ── discovery: directories, news and associations aren't firms ──

@pytest.mark.parametrize("domain", ["sfgate.com", "bizjournals.com",
                                    "lawyers.findlaw.com", "attorneys.superlawyers.com",
                                    "bestlawyers.com", "aarp.org", "naela.org", "massnaela.com"])
def test_directories_news_and_associations_are_junk(domain):
    from harvey.collectors.discover import is_junk
    assert is_junk(domain)


@pytest.mark.parametrize("domain", ["pryorlawyers.example", "yourelderlawyers.example",
                                    "trustandestateslawyers.example", "patchettlaw.example"])
def test_firms_with_lawyers_in_the_domain_are_not(domain):
    from harvey.collectors.discover import is_junk
    assert not is_junk(domain)


def test_a_tagline_in_og_site_name_is_dropped():
    from bs4 import BeautifulSoup  # noqa: F401
    from harvey.collectors.discover import business_name
    assert business_name("Pryor Law Firm | Boutique Law Firm Serving NY, NJ & AZ",
                         "pryorlawfirm.example") == "Pryor Law Firm"


# ── addresses printed next to a name, and firms that use initials ──

TEAM_PAGE = """
<div class="card"><h3>Mara J. Hollis</h3><p>Partner</p><p>555-0100</p>
  <a href="mailto:mjh@hollislaw.example">Email</a></div>
<div class="card"><h3>Dana Pryor</h3><p>Associate</p><p>dkp@hollislaw.example</p></div>
<div class="card"><h3>Theo Brightwater</h3><p>Of Counsel</p></div>
<p>General questions: info@hollislaw.example</p>
"""
PEOPLE = [("Mara", "Hollis"), ("Dana", "Pryor"), ("Theo", "Brightwater")]


def test_an_address_printed_under_a_name_is_theirs():
    from harvey.integrations.email_finder import addresses_next_to_names
    tied = addresses_next_to_names([TEAM_PAGE], PEOPLE, "hollislaw.example")
    assert sorted(tied) == [("dkp@hollislaw.example", "Dana", "Pryor"),
                            ("mjh@hollislaw.example", "Mara", "Hollis")]


def test_an_address_that_does_not_fit_the_name_above_is_not_tied():
    from harvey.integrations.email_finder import addresses_next_to_names
    page = "<h3>Dana Pryor</h3><p>Associate</p><p>zq@hollislaw.example</p>"
    assert addresses_next_to_names([page], PEOPLE, "hollislaw.example") == []


@pytest.mark.parametrize("local, ok", [("mjh", True), ("mh", True), ("mjhj", True), ("mxyz", False),
                                       ("jmh", False), ("m", False), ("mara", False)])
def test_initials_fit(local, ok):
    from harvey.integrations.email_finder import initials_fit
    assert initials_fit(local, "Mara", "Hollis") is ok


@pytest.mark.asyncio
async def test_a_published_address_is_used_and_nobody_else_is_guessed(monkeypatch):
    from harvey.integrations import email_finder as ef
    checked = []

    async def mx(domain):
        return "mx.example.net"

    async def pattern(domain, **kw):
        return "{f}.{l}", "default", 0.3

    async def verify(email, mx_type, env):
        checked.append(email)
        return "verified", False
    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "derive_pattern", pattern)
    monkeypatch.setattr(ef, "_verify_candidate", verify)
    known = [("mjh@hollislaw.example", "Mara", "Hollis")]
    mara = await ef.find_email("Mara", "Hollis", "hollislaw.example", known_emails=known, no_guess=True)
    theo = await ef.find_email("Theo", "Brightwater", "hollislaw.example", known_emails=known,
                               no_guess=True)
    assert (mara.email, mara.status) == ("mjh@hollislaw.example", "verified")
    assert theo.email == "" and checked == ["mjh@hollislaw.example"]


def test_a_search_phrase_is_not_a_company_name():
    from harvey.collectors.discover import business_name
    assert business_name("Texas sales tax attorney near me | Pryor Tax Law",
                         "pryortax.example") == "Pryor Tax Law"


def test_document_page_words_are_not_a_person():
    assert split_person_name("Date Approved") is None


@pytest.mark.asyncio
async def test_one_failed_round_of_retries_is_enough_for_a_domain(monkeypatch):
    from harvey.integrations import email_finder as ef
    monkeypatch.setattr(ef, "_RETRIES_FAILED", set())
    checked = []

    async def mx(domain):
        return "mx.example.net"

    async def pattern(domain, **kw):
        return "{f}.{l}", "default", 0.3

    async def verify(email, mx_type, env):
        checked.append(email)
        return "invalid", False
    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "derive_pattern", pattern)
    monkeypatch.setattr(ef, "_verify_candidate", verify)
    await ef.find_email("Mara", "Hollis", "hollislaw.example")
    assert len(checked) == 3
    await ef.find_email("Dana", "Pryor", "hollislaw.example")
    assert checked[3:] == ["dana.pryor@hollislaw.example"]      # no retries this time


@pytest.mark.asyncio
async def test_the_decision_maker_is_written_to_before_a_paralegal(state):  # noqa: F811
    import aiosqlite
    from harvey.agents.writer import Writer
    from harvey.models.company import Company
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await seed_prospect(state, email="pat@acme.example")
    await seed_prospect(state, email="mara@acme.example")
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE prospects SET company_id = ?, email_status = 'risky'", (cid,))
        await db.execute("UPDATE prospects SET title = 'Paralegal' WHERE email = 'pat@acme.example'")
        await db.execute("UPDATE prospects SET title = 'Founder & Managing Partner' "
                         "WHERE email = 'mara@acme.example'")
        await db.commit()
    writer = Writer(brain=None, state=state, config=_cfg(provider="instantly"))
    kept = await writer._one_per_company(await state.get_prospects_by_status("new"))
    assert [p.email for p in kept] == ["mara@acme.example"]


@pytest.mark.parametrize("name, keyword", [
    ("Austin Tax Attorney", True), ("AZ Tax Attorneys", True), ("Pryor's First Elder Law Firm", True),
    ("#1 Tax Lawyers", True), ("Pryor Tax Law", False), ("Hollis & Kerr", False),
    ("First Republic Law", False), ("Brightwater Wealth Partners", False),
])
def test_search_keywords_and_slogans_are_not_names(name, keyword):
    from harvey.collectors.discover import looks_like_keyword
    assert looks_like_keyword(name) is keyword


def test_brand_name_prefers_the_declared_name_over_a_keyword_title():
    from harvey.collectors.discover import brand_name
    html = ('<meta property="og:site_name" content="Brightwater Law PLC">'
            '<title>Springfield Tax Attorneys | Brightwater Law PLC</title>')
    assert brand_name(html, "springfieldtax.example") == "Brightwater Law PLC"
    assert brand_name("<title>Springfield Tax Attorney | Pryor Tax Law</title>",
                      "pryortaxlaw.example") == "Pryor Tax Law"
