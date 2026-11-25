"""Firm phone numbers read from the firm's own site (harvey/collectors/phones.py)."""

import pytest

from harvey.collectors import phones
from harvey.collectors.phones import best_phone, normalize, phones_on_page
from harvey.state import Company, Prospect, StateManager


@pytest.mark.parametrize("raw,expected", [
    ("(404) 555-1234", "(404) 555-1234"),
    ("404.555.1234", "(404) 555-1234"),
    ("+1 404 555 1234", "(404) 555-1234"),
    ("1-404-555-1234", "(404) 555-1234"),
    ("+14045551234", "(404) 555-1234"),
    ("555-1234", ""),                  # no area code
    ("(104) 555-1234", ""),            # area codes never start with 0 or 1
    ("(404) 155-1234", ""),            # nor do exchanges
    ("(404) 911-1234", ""),            # N11
    ("(404) 555-0123", ""),            # reserved for fiction
    ("1-800-LAW-FIRM", ""),            # vanity numbers aren't decoded
    ("+44 20 7946 0958", ""),          # not North American
    ("", ""),
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_tel_link_beats_printed_text():
    html = """<p>Call our billing desk at 212-555-9000</p>
              <a href="tel:+14045551234">Call us</a>"""
    assert phones_on_page(html)[0] == "(404) 555-1234"


def test_schema_telephone_is_read():
    html = '<script type="application/ld+json">{"@type":"Attorney","telephone":"+1-713-555-2020"}</script>'
    assert phones_on_page(html) == ["(713) 555-2020"]


def test_number_repeated_in_header_and_footer_wins():
    html = "<header>602-555-7000</header><p>Old line 602-555-8000</p><footer>(602) 555-7000</footer>"
    assert phones_on_page(html)[0] == "(602) 555-7000"


def test_local_number_beats_toll_free():
    html = '<a href="tel:8005552000">800</a><a href="tel:8005552000">800</a><p>Office: 214-555-3000</p>'
    assert phones_on_page(html)[0] == "(214) 555-3000"


def test_fax_is_left_out():
    html = "<p>Phone: 770-555-1000<br>Fax: 770-555-1001</p><p>F: (770) 555-1002</p>"
    assert phones_on_page(html) == ["(770) 555-1000"]


def test_numbers_inside_scripts_and_long_digit_runs_are_ignored():
    html = """<script>var id = "404-555-9999";</script>
              <p>Order 20264045551234 shipped. Tracking 404-555-1234-88.</p>"""
    assert phones_on_page(html) == []


def test_best_phone_takes_the_first_page_that_has_one():
    assert best_phone("<p>no number here</p>", "<p>Call 305-555-4000</p>") == "(305) 555-4000"
    assert best_phone("", "") == ""


class FakeCollector:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _resolve_site(self, domain):
        self.asked.append(domain)
        home = self.pages.get(domain, "")
        return (f"https://{domain}", home) if home else ("", "")

    async def _first_present(self, site, paths):
        return site + "/contact", self.pages.get(site + "/contact", "")


async def _company(state, name, domain, phone=""):
    return await state.add_company(Company(name=name, domain=domain, phone=phone))


async def _phone_of(state, table, row_id):
    async with state._connect() as db:
        async with db.execute(f"SELECT phone FROM {table} WHERE id = ?", (row_id,)) as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_stage_saves_firm_number_and_copies_it_to_contacts(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "harvey.db"))
    await state.init_db()
    home_has = await _company(state, "Home Law", "homelaw.com")
    contact_has = await _company(state, "Contact Law", "contactlaw.com")
    nothing = await _company(state, "Quiet Law", "quietlaw.com")
    down = await _company(state, "Down Law", "downlaw.com")
    listed = await _company(state, "Listed Law", "listedlaw.com", phone="+1281-555-7775")
    person = await state.add_prospect(Prospect(first_name="Ann", last_name="Lee",
                                               email="ann@homelaw.com", company_id=home_has))
    direct = await state.add_prospect(Prospect(first_name="Bo", last_name="Ray", phone="(404) 555-2222",
                                               email="bo@homelaw.com", company_id=home_has))
    at_listed = await state.add_prospect(Prospect(first_name="Cy", last_name="Orr",
                                                  email="cy@listedlaw.com", company_id=listed))
    fake = FakeCollector({
        "homelaw.com": '<a href="tel:4045551234">Call</a>',
        "contactlaw.com": "<p>Welcome</p>",
        "https://contactlaw.com/contact": "<p>Office 713-555-2020 | Fax 713-555-2021</p>",
        "quietlaw.com": "<p>Welcome</p>",
    })
    import harvey.collectors.profile as profile
    monkeypatch.setattr(profile, "ProfileCollector", lambda *a, **k: fake)
    async def no_sleep(*_): return None
    monkeypatch.setattr(phones.asyncio, "sleep", no_sleep)

    checked, found = await phones.run_phone_stage(state, limit=10)

    assert (checked, found) == (4, 2)                      # the listed firm already had one
    assert "listedlaw.com" not in fake.asked
    assert await _phone_of(state, "companies", home_has) == "(404) 555-1234"
    assert await _phone_of(state, "companies", contact_has) == "(713) 555-2020"
    assert await _phone_of(state, "companies", nothing) == ""
    assert await _phone_of(state, "companies", down) == ""
    assert await _phone_of(state, "companies", listed) == "+1281-555-7775"   # never overwritten
    assert await _phone_of(state, "prospects", person) == "(404) 555-1234"
    assert await _phone_of(state, "prospects", direct) == "(404) 555-2222"   # own number kept
    assert await _phone_of(state, "prospects", at_listed) == "+1281-555-7775"

    # Looked-at firms rotate out: a second pass has nothing left to visit.
    fake.asked.clear()
    assert await phones.run_phone_stage(state, limit=10) == (0, 0)
    assert fake.asked == []
