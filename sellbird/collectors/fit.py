"""Does a company's own site say it is in one of the ICP's industries?

Searches for "estate planning law firms" also return the software vendors,
directories, banks and newspapers that write about them. Before anyone at a
company is contacted, its pages must read like the business itself: a law
firm's practice areas and attorneys, an advisor's wealth management, an
agency's quotes. Vendors that sell *to* those businesses (a demo, a free
trial, "for law firms") are not one of them.

Only industries Harvey has words for are checked; for any other ICP the
check passes, rather than guessing.
"""

import re

# What a business in each family says about itself on its own site.
FAMILIES = {
    "law": (re.compile(r"(?i)\blaw|legal|attorney|lawyer|counsel|probate|esq\b"), re.compile(
        r"(?i)\b(?:practice areas?|our attorneys|attorneys? at law|law (?:firm|offices?|group)|"
        r"free consultation|schedule a consultation|legal services|esq\.?|"
        r"estate planning attorney|probate attorney|elder law attorney|tax attorney)\b")),
    "finance": (re.compile(r"(?i)financ|wealth|advis|invest|retirement|fiduciar"), re.compile(
        r"(?i)\b(?:wealth management|financial plann(?:ing|er)|financial advis[oe]rs?|fiduciary|"
        r"cfp|registered investment advis[oe]r|investment advis(?:ory|ers?|ors?)|retirement planning)\b")),
    "insurance": (re.compile(r"(?i)insur"), re.compile(
        r"(?i)\b(?:insurance agency|independent (?:insurance )?agen(?:cy|ts?)|get a quote|"
        r"free quote|insurance agents?|coverage options|life insurance|auto insurance)\b")),
}

# Said only by a bank or a newsroom: one is enough. ("Not FDIC insured" in
# an advisor's disclosure is not "Member FDIC"; and firms list their bar
# memberships, so the bar only counts against a site below.)
NEVER_THE_BUSINESS = re.compile(
    r"(?i)\b(?:member fdic|equal housing lender|breaking news)\b")

# A vendor, publication, bank or association that writes about the industry
# without being in it.
NOT_THE_BUSINESS = re.compile(
    r"(?i)\b(?:book a demo|request a demo|schedule a demo|start (?:your )?free trial|free trial|"
    r"pricing plans|saas|software for|platform for|api docs|for law firms|for attorneys|"
    r"breaking news|subscribe to (?:our )?newsletter|member fdic|credit union|"
    r"bar association|state bar|501\(c\)\(3\))\b")


def families(industries) -> list[str]:
    """The families Harvey can check for these ICP industries."""
    found = []
    for name, (topic, _) in FAMILIES.items():
        if any(topic.search(i or "") for i in industries or []):
            found.append(name)
    return found


def industry_fit(text: str, industries) -> tuple[bool | None, str]:
    """(fits, reason) for a company whose pages read ``text``.

    None when the ICP's industries aren't ones Harvey has words for: the
    caller then lets the company through."""
    wanted = families(industries)
    if not wanted:
        return None, "industries Harvey can't check"
    text = text or ""
    own = {name: len(FAMILIES[name][1].findall(text)) for name in wanted}
    if hard := NEVER_THE_BUSINESS.search(text):
        return False, f"its site says \"{hard.group(0)}\": not a firm"
    against = len(NOT_THE_BUSINESS.findall(text))
    best = max(own, key=own.get)
    if own[best] == 0:
        return False, "its site doesn't describe " + " or ".join(wanted) + " services"
    if against >= 2 and against >= own[best]:
        return False, "its site reads like a vendor, publication or association, not a firm"
    return True, f"{best} ({own[best]} signs)"
