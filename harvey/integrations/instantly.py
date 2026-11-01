"""Instantly.ai API client for cold email campaigns."""

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger("harvey.instantly")

BASE_URL = "https://api.instantly.ai/api/v2"

# Retry policy
MAX_RETRIES = 4
BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Explicit timeouts: connect / read / write / pool
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


class InstantlyClient:
    def __init__(self, api_key: str):
        self.api_key = api_key or ""
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _redact(self, text: str) -> str:
        """Strip the API key from anything we log or raise."""
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff with jitter; honors Retry-After on 429."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), BACKOFF_CAP) + random.uniform(0, 1)
                except ValueError:
                    pass
        base = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
        return base * random.uniform(0.5, 1.5)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list | None:
        """Make an authenticated request with retries, backoff, and validation."""
        if not self.api_key:
            logger.error("Instantly API key is not configured.")
            return None

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.request(
                        method, url, headers=self.headers, json=data, params=params
                    )
                except httpx.HTTPError as e:
                    # Network/timeout errors are retryable.
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, None)
                        logger.warning(
                            f"Instantly request error ({method} {endpoint}): "
                            f"{self._redact(str(e))}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly request failed after {MAX_RETRIES} retries "
                        f"({method} {endpoint}): {self._redact(str(e))}"
                    )
                    return None

                if response.status_code in RETRYABLE_STATUS:
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, response)
                        label = (
                            "rate limited (429)"
                            if response.status_code == 429
                            else f"server error ({response.status_code})"
                        )
                        logger.warning(
                            f"Instantly {label} on {method} {endpoint}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint} after {MAX_RETRIES} retries: "
                        f"{self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code >= 400:
                    # Non-retryable client error (400, 401, 403, 404, ...)
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint}: {self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code == 204 or not response.content:
                    return {}

                try:
                    payload = response.json()
                except ValueError:
                    logger.error(
                        f"Instantly returned non-JSON response on {method} "
                        f"{endpoint}: {self._redact(response.text[:200])}"
                    )
                    return None

                if not isinstance(payload, (dict, list)):
                    logger.error(
                        f"Instantly returned unexpected JSON type "
                        f"({type(payload).__name__}) on {method} {endpoint}."
                    )
                    return None
                return payload

        return None

    @staticmethod
    def _extract_items(result: Any) -> list:
        """Normalize list responses: API may return a bare list or {items: [...]}. """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            items = result.get("items") or result.get("data")
            if isinstance(items, list):
                return items
        return []

    # ── Campaigns ──

    async def active_account_emails(self) -> list[str]:
        """The workspace's sending mailboxes that are connected and active."""
        result = await self._request("GET", "/accounts", params={"limit": 100, "status": 1})
        return [a["email"] for a in self._extract_items(result)
                if isinstance(a, dict) and a.get("email")]

    async def create_campaign(
        self,
        name: str,
        timezone: str = "UTC",
        daily_limit: int | None = None,
    ) -> dict | None:
        """Create a campaign that sends from every active mailbox, weekdays
        9-5 in ``timezone``. Returns campaign data with id."""
        accounts = await self.active_account_emails()
        if not accounts:
            logger.error("Instantly has no active sending accounts; can't create a campaign.")
            return None
        body = {
            "name": name,
            "campaign_schedule": {"schedules": [{
                "name": "Weekdays",
                "timing": {"from": "09:00", "to": "17:00"},
                "days": {str(d): True for d in range(1, 6)},
                "timezone": instantly_timezone(timezone),
            }]},
            "email_list": accounts,
            # stop_for_company: a reply from anyone at a company stops the
            # sequence to everyone there, as on Harvey's own providers.
            "stop_for_company": True,
            "stop_on_reply": True,
            # Plain text, as written: no tracking pixel or rewritten links.
            "text_only": True,
            "open_tracking": False,
            "link_tracking": False,
        }
        if daily_limit:
            body["daily_limit"] = daily_limit
        result = await self._request("POST", "/campaigns", body)
        if isinstance(result, dict):
            logger.info(f"Created campaign: {name} (id={result.get('id')}, "
                        f"{len(accounts)} sending accounts)")
            return result
        return None

    async def get_campaign(self, campaign_id: str) -> dict | None:
        """Get campaign details."""
        result = await self._request("GET", f"/campaigns/{campaign_id}")
        return result if isinstance(result, dict) else None

    async def list_campaigns(self, limit: int = 100) -> list:
        """List all campaigns."""
        result = await self._request("GET", "/campaigns", params={"limit": limit})
        return self._extract_items(result)

    async def set_daily_limit(self, campaign_id: str, daily_limit: int) -> dict | None:
        """Set how many emails a campaign may send a day."""
        result = await self._request(
            "PATCH", f"/campaigns/{campaign_id}", {"daily_limit": daily_limit})
        return result if isinstance(result, dict) else None

    async def activate_campaign(self, campaign_id: str) -> dict | None:
        """Start a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/activate", {})
        return result if isinstance(result, dict) else None

    async def pause_campaign(self, campaign_id: str) -> dict | None:
        """Pause a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/pause", {})
        return result if isinstance(result, dict) else None

    # ── Campaign Emails (Sequences) ──

    async def set_campaign_emails(
        self,
        campaign_id: str,
        sequences: list[dict],
    ) -> dict | None:
        """Set the email sequence for a campaign.

        sequences format (Harvey's: ``wait`` is days after the previous step):
        [
            {
                "subject": "Subject line",
                "body": "Plain-text body",
                "wait": 0
            },
            ...
        ]
        """
        result = await self._request(
            "PATCH",
            f"/campaigns/{campaign_id}",
            {"sequences": [{"steps": instantly_steps(sequences)}]},
        )
        return result if isinstance(result, dict) else None

    # ── Leads ──

    async def add_leads(
        self,
        campaign_id: str,
        leads: list[dict],
    ) -> dict | None:
        """Add leads to a campaign.

        leads format:
        [
            {
                "email": "john@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "company_name": "Acme Inc",
                "custom_variables": {"custom_var": "value"}
            }
        ]
        """
        if not leads:
            logger.warning("add_leads called with an empty lead list; skipping.")
            return {}
        result = None
        for start in range(0, len(leads), LEADS_PER_REQUEST):
            result = await self._request(
                "POST",
                "/leads/add",
                # Someone already in another of the user's campaigns isn't
                # emailed twice.
                {"campaign_id": campaign_id,
                 "leads": leads[start:start + LEADS_PER_REQUEST],
                 "skip_if_in_workspace": True},
            )
            if not isinstance(result, dict):
                return None
        return result

    async def delete_lead(self, email: str) -> int | None:
        """Remove ``email`` from every campaign in the workspace, so nothing
        more is sent to it. Returns how many leads went, or None on failure."""
        email = (email or "").strip().lower()
        result = await self._request("POST", "/leads/list", {"search": email, "limit": 100})
        if result is None:
            return None
        deleted = 0
        for lead in self._extract_items(result):
            if not isinstance(lead, dict) or (lead.get("email") or "").lower() != email:
                continue
            # Instantly refuses a JSON request with an empty body.
            if await self._request("DELETE", f"/leads/{lead['id']}", {}) is None:
                return None
            deleted += 1
        return deleted

    # ── Replies / Emails ──

    async def get_campaign_emails_sent(
        self, campaign_id: str, limit: int = 100
    ) -> list:
        """Get emails sent for a campaign."""
        result = await self._request(
            "GET", "/emails",
            params={"campaign_id": campaign_id, "email_type": "sent", "limit": limit},
        )
        return self._extract_items(result)

    async def get_replies(self, campaign_id: str | None = None) -> list:
        """Get replies, optionally filtered by campaign, as
        {uuid, lead_email, body, subject} dicts."""
        params = {"email_type": "received", "limit": 100}
        if campaign_id:
            params["campaign_id"] = campaign_id
        result = await self._request("GET", "/emails", params=params)
        replies = []
        for email in self._extract_items(result):
            if not isinstance(email, dict):
                continue
            body = email.get("body")
            text = body.get("text") if isinstance(body, dict) else body
            replies.append({
                "uuid": email.get("id") or "",
                "lead_email": email.get("lead") or email.get("from_address_email") or "",
                "body": text or email.get("content_preview") or "",
                "subject": email.get("subject") or "",
            })
        return replies

    async def send_reply(
        self,
        reply_to_uuid: str,
        body: str,
    ) -> dict | None:
        """Send a reply to a lead's email, from the mailbox it was sent to."""
        original = await self._request("GET", f"/emails/{reply_to_uuid}")
        if not isinstance(original, dict) or not original.get("eaccount"):
            logger.error(f"Instantly: can't find the email {reply_to_uuid} to reply to.")
            return None
        subject = original.get("subject") or ""
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        result = await self._request(
            "POST",
            "/emails/reply",
            {"reply_to_uuid": reply_to_uuid, "eaccount": original["eaccount"],
             "subject": subject, "body": {"text": body, "html": text_to_html(body)}},
        )
        return result if isinstance(result, dict) else None

    # ── Analytics ──

    async def get_campaign_analytics(self, campaign_id: str) -> dict | None:
        """Get campaign performance stats."""
        result = await self._request("GET", "/campaigns/analytics", params={"id": campaign_id})
        items = self._extract_items(result)
        return items[0] if items and isinstance(items[0], dict) else None

    # ── Account / Warmup ──

    async def list_accounts(self) -> list:
        """List connected email accounts."""
        result = await self._request("GET", "/accounts", params={"limit": 100})
        return self._extract_items(result)


# Instantly's names for Harvey's merge tags. The rest ({{company}},
# {{title}}, {{personalization}}) travel as lead variables of the same name.
INSTANTLY_TAGS = {"first_name": "firstName", "last_name": "lastName"}

LEADS_PER_REQUEST = 1000

# Instantly accepts only these zones for a schedule; any other is matched to
# one that keeps the same offsets through the year.
INSTANTLY_TIMEZONES = (
    "America/Detroit", "America/Chicago", "America/Boise", "America/Creston",
    "America/Dawson", "America/Anchorage", "Etc/GMT+10", "America/Regina",
    "America/Bogota", "America/Caracas", "America/Sao_Paulo", "America/St_Johns",
    "Europe/Isle_of_Man", "Africa/Abidjan", "Europe/Belgrade", "Africa/Algiers",
    "Europe/Helsinki", "Europe/Istanbul", "Asia/Dubai", "Asia/Karachi",
    "Asia/Kolkata", "Asia/Dhaka", "Asia/Hong_Kong", "Australia/Perth",
    "Asia/Taipei", "Australia/Brisbane", "Australia/Melbourne",
    "Pacific/Auckland", "Etc/GMT+12",
)


def instantly_timezone(name: str) -> str:
    """The Instantly schedule zone that matches IANA zone ``name``."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if name in INSTANTLY_TIMEZONES:
        return name
    # Instantly's list has no zone that still keeps US Pacific time; its
    # Pacific entry is America/Dawson.
    if name in ("America/Los_Angeles", "America/Vancouver", "America/Tijuana", "US/Pacific"):
        return "America/Dawson"
    try:
        zone = ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return "America/Chicago"

    def offsets(z):
        return tuple(datetime(2026, month, 1, 12, tzinfo=z).utcoffset() for month in (1, 7))

    want = offsets(zone)
    for candidate in INSTANTLY_TIMEZONES:
        if offsets(ZoneInfo(candidate)) == want:
            return candidate
    return "Africa/Abidjan" if want == offsets(ZoneInfo("UTC")) else "America/Chicago"


def text_to_html(text: str) -> str:
    """Plain text as the HTML Instantly sends: escaped, a <div> per line as
    its editor writes it. Instantly drops text that sits outside an element."""
    import html
    return "".join(
        f"<div>{html.escape(line, quote=False)}</div>" if line.strip() else "<div><br /></div>"
        for line in (text or "").split("\n")
    )


def instantly_steps(sequence: list[dict]) -> list[dict]:
    """Harvey's steps as Instantly's. Harvey's ``wait`` is days after the
    previous email; Instantly's ``delay`` is days before the next one."""
    steps = []
    for i, step in enumerate(sequence):
        subject, body = step.get("subject") or "", step.get("body") or ""
        for ours, theirs in INSTANTLY_TAGS.items():
            for tag in ("{{" + ours + "}}", "{{ " + ours + " }}"):
                subject = subject.replace(tag, "{{" + theirs + "}}")
                body = body.replace(tag, "{{" + theirs + "}}")
        following = sequence[i + 1].get("wait", 0) if i + 1 < len(sequence) else 0
        steps.append({
            "type": "email",
            "delay": max(int(following or 0), 0),
            "delay_unit": "days",
            "variants": [{"subject": subject, "body": text_to_html(body)}],
        })
    return steps
