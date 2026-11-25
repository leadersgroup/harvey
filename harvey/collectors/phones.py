"""Phone numbers for the firms Harvey already knows.

A firm's main line is on its own website: in a ``tel:`` link, in its
schema.org markup, or printed in the header, footer or contact page. This
reads it from there. One or two plain HTTP requests per firm, no browser and
no model call, so it rides along on every heartbeat like profiling does.

What is saved is the firm's published number. It is stored on the company
and copied to that company's contacts who have no number of their own, so the
Contacts list and exports show something to call. It is a main line, not a
person's direct line, and ``phone_verified`` stays off.

Which firms have been looked at is kept in a small table of its own
(``company_phone_checks``), created here on demand, so this never touches the
numbered schema migrations in ``state.py``.

North American numbers only: that is the market the ICP checks already
restrict Harvey to.
"""

import asyncio
import logging
import random
import re
from collections import Counter

logger = logging.getLogger("harvey.phones")

CONTACT_PATHS = ("/contact", "/contact-us", "/contact/", "/contact-us/", "/contact.html")
CONCURRENT_SITES = 4
TOLL_FREE = {"800", "822", "833", "844", "855", "866", "877", "888"}

_TEL_HREF = re.compile(r"""href\s*=\s*["']\s*tel:([^"']+)""", re.I)
_SCHEMA_TEL = re.compile(r'"telephone"\s*:\s*"([^"]+)"', re.I)
_CODE = re.compile(r"<(script|style|noscript|svg)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
# (404) 555-1234 · 404-555-1234 · 404.555.1234 · +1 404 555 1234 · 1-404-555-1234
_PRINTED = re.compile(
    r"(?<![\d\-.(/])(?:\+?1[\s.\-]{0,2})?\(?([2-9]\d{2})\)?[\s.\-]{1,3}([2-9]\d{2})[\s.\-]{1,2}(\d{4})(?![\d\-])")
_FAX_BEFORE = re.compile(r"(?:\bfax\b|\bfacsimile\b|\bf\s*[:.|])[^\d]{0,12}$", re.I)

_CHECKS_TABLE = """
CREATE TABLE IF NOT EXISTS company_phone_checks (
    company_id TEXT PRIMARY KEY,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    found INTEGER DEFAULT 0
)
"""


def normalize(raw: str) -> str:
    """``(404) 555-1234`` for a valid North American number, else ``""``."""
    if re.search(r"[A-Za-z]{3,}", raw or ""):
        return ""  # vanity numbers (1-800-LAW-FIRM) aren't decoded
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    area, exchange, line = digits[:3], digits[3:6], digits[6:]
    if area[0] in "01" or exchange[0] in "01":
        return ""
    if area[1:] == "11" or exchange[1:] == "11":
        return ""  # N11 service codes
    if exchange == "555" and line.startswith("01"):
        return ""  # reserved for fiction
    if len(set(digits)) == 1:
        return ""
    return f"({area}) {exchange}-{line}"


def phones_on_page(html: str) -> list[str]:
    """Every phone number a page publishes, best candidate first.

    A number the site itself marks up (a ``tel:`` link, schema.org
    ``telephone``) outranks one that merely appears in the text; one repeated
    across header and footer outranks a single mention; a local number
    outranks a toll-free one; a number labelled as a fax is left out.
    """
    if not html:
        return []
    score: Counter = Counter()
    order: dict[str, int] = {}

    def add(raw: str, weight: int):
        number = normalize(raw)
        if number:
            score[number] += weight
            order.setdefault(number, len(order))

    for match in _TEL_HREF.finditer(html):
        add(match.group(1).split(";")[0].split(",")[0], 3)
    for match in _SCHEMA_TEL.finditer(html):
        add(match.group(1), 3)
    marked_up = set(score)
    text = _TAG.sub(" ", _CODE.sub(" ", html))
    text = re.sub(r"&nbsp;|&#160;|\u00a0", " ", text)
    faxes = set()
    for match in _PRINTED.finditer(text):
        if _FAX_BEFORE.search(text[max(0, match.start() - 24):match.start()]):
            faxes.add(normalize(match.group(0)))
        else:
            add(match.group(0), 1)
    # A number labelled as a fax is dropped unless the site itself links it
    # as a phone, which outranks a nearby word.
    for fax in faxes - marked_up:
        score.pop(fax, None)

    def rank(number: str):
        return (number[1:4] in TOLL_FREE, -score[number], order[number])

    return sorted(score, key=rank)


def best_phone(*pages: str) -> str:
    """The first page that publishes a number decides."""
    for html in pages:
        found = phones_on_page(html)
        if found:
            return found[0]
    return ""


async def _ensure_table(state):
    async with state._connect() as db:
        await db.execute(_CHECKS_TABLE)
        await db.commit()


async def sync_contact_phones(state) -> int:
    """Give contacts without a number their firm's published one."""
    async with state._connect() as db:
        cursor = await db.execute(
            """UPDATE prospects
                  SET phone = (SELECT c.phone FROM companies c WHERE c.id = prospects.company_id)
                WHERE COALESCE(phone, '') = ''
                  AND company_id IN (SELECT id FROM companies WHERE COALESCE(phone, '') != '')""")
        await db.commit()
        return cursor.rowcount or 0


async def companies_needing_phone(state, limit: int = 30) -> list[dict]:
    """Firms with a website, no number yet, and not looked at before.
    Firms Harvey already has contacts at come first."""
    await _ensure_table(state)
    async with state._connect() as db:
        async with db.execute(
            """SELECT c.id, c.domain FROM companies c
                WHERE COALESCE(c.domain, '') != '' AND COALESCE(c.phone, '') = ''
                  AND c.id NOT IN (SELECT company_id FROM company_phone_checks)
                ORDER BY EXISTS (SELECT 1 FROM prospects p WHERE p.company_id = c.id) DESC,
                         c.created_at DESC
                LIMIT ?""", (int(limit),)) as cursor:
            return [{"id": row[0], "domain": row[1]} for row in await cursor.fetchall()]


async def _record(state, company_id: str, phone: str):
    async with state._connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO company_phone_checks (company_id, found) VALUES (?, ?)",
            (company_id, 1 if phone else 0))
        if phone:
            await db.execute(
                """UPDATE companies SET phone = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND COALESCE(phone, '') = ''""", (phone, company_id))
        await db.commit()


async def find_company_phone(collector, domain: str) -> str:
    """Homepage first; the contact page only when the homepage has none."""
    domain = domain.lower().replace("www.", "").strip("/")
    site, home = await collector._resolve_site(domain)
    if not home:
        return ""
    phone = best_phone(home)
    if not phone:
        _, contact = await collector._first_present(site, CONTACT_PATHS)
        phone = best_phone(contact)
    return phone


async def run_phone_stage(state, limit: int = 30) -> tuple[int, int]:
    """Look up numbers for the next ``limit`` firms. Returns (checked, found).

    Every firm is marked as looked at whatever the outcome, so an unreachable
    site rotates out instead of being retried every cycle.
    """
    from harvey.collectors.profile import ProfileCollector

    pending = await companies_needing_phone(state, limit)
    found = 0
    if pending:
        sites = asyncio.Semaphore(CONCURRENT_SITES)
        writes = asyncio.Lock()
        async with ProfileCollector(state, set()) as collector:

            async def one(company):
                nonlocal found
                async with sites:
                    try:
                        phone = await find_company_phone(collector, company["domain"])
                    except Exception as e:
                        logger.debug(f"phones: {company['domain']} failed: {e}")
                        phone = ""
                    async with writes:
                        await _record(state, company["id"], phone)
                    if phone:
                        found += 1
                    await asyncio.sleep(random.uniform(0.2, 0.6))

            await asyncio.gather(*(one(c) for c in pending))
    copied = await sync_contact_phones(state)
    if pending or copied:
        logger.info(f"phones: {found} of {len(pending)} firm(s) had a number on their site; "
                    f"{copied} contact(s) updated")
    return len(pending), found
