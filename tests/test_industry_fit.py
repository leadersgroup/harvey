"""The scout only contacts companies whose own site reads like one of the
ICP's industries: not the vendors, newspapers or banks a search also finds."""

import pytest

from harvey.collectors.fit import families, industry_fit

ICP = ["Estate Planning Law Firms", "Tax Law Firms", "Financial Advisory & Wealth Management Firms"]


@pytest.mark.parametrize("text", [
    "Hollis & Kerr, PLLC. Practice Areas: Wills, Trusts, Probate. Our Attorneys. Free consultation.",
    "Pryor Law Office: estate planning attorney serving Springfield. Schedule a consultation.",
    "Brightwater Wealth: fiduciary financial planning and wealth management for families.",
    # A firm that lists its bar memberships is still a firm.
    "Quillmont Law Group. Our attorneys are members of the State Bar Association. Practice areas: tax.",
])
def test_a_firms_own_site_fits(text):
    assert industry_fit(text, ICP)[0] is True


@pytest.mark.parametrize("text", [
    "Case management software for law firms. Book a demo. Start your free trial. Pricing plans.",
    "Breaking news, weather and sports for Springfield. Subscribe to our newsletter.",
    "Checking, savings and wealth management. Member FDIC. Equal Housing Lender.",
    "Lead data for sales teams. Request a demo.",
])
def test_vendors_newsrooms_and_banks_do_not(text):
    assert industry_fit(text, ICP)[0] is False


def test_an_icp_harvey_has_no_words_for_is_not_judged():
    assert families(["Roofing Contractors"]) == []
    assert industry_fit("anything at all", ["Roofing Contractors"])[0] is None


def test_the_families_come_from_the_icp():
    assert families(ICP) == ["law", "finance"]
    assert families(["Independent Insurance Agencies"]) == ["insurance"]


@pytest.mark.asyncio
async def test_the_scout_contacts_nobody_at_a_vendor():
    from types import SimpleNamespace
    from harvey.agents.scout import Scout
    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=SimpleNamespace(industries=ICP))
    looked_for_people = []

    async def read_site(domain):
        return {"pages": [("https://vendor.example/",
                           "<h1>Software for law firms</h1><p>Book a demo. Free trial.</p>")]}

    async def team(domain):
        looked_for_people.append(domain)
        return []
    scout._read_site = read_site
    scout._scrape_team_page = team
    assert await scout._contacts_from_company("c1", "vendor.example", "Vendor", "Estate Planning") == []
    assert looked_for_people == []
