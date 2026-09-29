import asyncio
import logging

import httpx

log = logging.getLogger("sas.discord")


def humanize_seconds(seconds):
    seconds = int(seconds)
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class DiscordNotifier:
    def __init__(self, settings):
        self.s = settings

    def _webhook_url(self, job_id):
        if not self.s.discord_notify_enabled:
            return None
        webhook_url = self.s.discord_webhook_url.strip()
        if not webhook_url:
            log.warning(
                "discord_notification_skipped job=%s reason=missing_webhook_url",
                job_id,
            )
            return None
        return webhook_url

    async def _post(self, job_id, webhook_url, payload, request):
        try:
            async with httpx.AsyncClient(
                timeout=self.s.discord_timeout_seconds
            ) as client:
                response = await client.post(webhook_url, json=payload)
                response.raise_for_status()
        except httpx.HTTPError as error:
            log.exception(
                "discord_notification_failed job=%s title=%r author=%r error=%s",
                job_id,
                request.title,
                request.author,
                error,
            )
            return False
        return True

    async def notify_audiobook_available(
        self, job_id, request, destination=None, retry_count=0
    ):
        webhook_url = self._webhook_url(job_id)
        if not webhook_url:
            return False

        delay_seconds = max(0, self.s.discord_notify_delay_seconds)
        if delay_seconds:
            log.info(
                "discord_notification_scheduled job=%s delay_seconds=%d",
                job_id,
                delay_seconds,
            )
            await asyncio.sleep(delay_seconds)

        fields = [
            {"name": "Author", "value": request.author, "inline": True},
            {"name": "Status", "value": "Ready to listen", "inline": True},
        ]
        if request.narrator:
            fields.append(
                {"name": "Narrator", "value": request.narrator, "inline": True}
            )
        if retry_count:
            fields.append(
                {
                    "name": "Found after",
                    "value": f"{retry_count} retr{'y' if retry_count == 1 else 'ies'}",
                    "inline": True,
                }
            )

        payload = {
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": "\U0001F4D6 New Audiobook Available!",
                    "description": request.title,
                    "color": 5763719,
                    "fields": fields,
                }
            ],
        }

        if not await self._post(job_id, webhook_url, payload, request):
            return False
        log.info(
            "discord_notification_sent job=%s title=%r author=%r destination=%r",
            job_id,
            request.title,
            request.author,
            destination,
        )
        return True

    async def notify_no_match(
        self, job_id, request, reason, retry_interval_seconds=None
    ):
        """Sent once per job when no good match is found. No delay."""
        if not self.s.no_match_notify_enabled:
            return False
        webhook_url = self._webhook_url(job_id)
        if not webhook_url:
            return False

        if retry_interval_seconds:
            status = f"Will keep searching (every {humanize_seconds(retry_interval_seconds)})"
        else:
            status = "Not retrying"

        fields = [
            {"name": "Author", "value": request.author, "inline": True},
            {"name": "Status", "value": status, "inline": True},
            {"name": "Reason", "value": str(reason)[:1000], "inline": False},
        ]
        payload = {
            "allowed_mentions": {"parse": []},
            "embeds": [
                {
                    "title": "\U0001F50E No Good Match Found",
                    "description": request.title,
                    "color": 15105570,
                    "fields": fields,
                }
            ],
        }

        if not await self._post(job_id, webhook_url, payload, request):
            return False
        log.info(
            "discord_no_match_notification_sent job=%s title=%r author=%r",
            job_id,
            request.title,
            request.author,
        )
        return True
