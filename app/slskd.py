import httpx, uuid, logging

log = logging.getLogger("sas.slskd")


class Client:
    def __init__(self, s):
        b = s.slskd_url.rstrip("/")
        u = s.slskd_url_base.strip("/")
        self.api = f"{b}/{u}/api/v0" if u else f"{b}/api/v0"
        self.c = httpx.AsyncClient(
            headers={"X-API-Key": s.slskd_api_key},
            verify=s.slskd_verify_ssl,
            timeout=max(30, s.search_timeout_seconds + 10),
        )

    async def close(self):
        await self.c.aclose()

    async def req(self, m, p, **kw):
        try:
            x = await self.c.request(m, self.api + p, **kw)
            x.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"slskd {m} {p}: {e}") from e
        if not x.content:
            return None
        try:
            return x.json()
        except ValueError:
            return x.text

    async def health(self):
        return await self.req("GET", "/application")

    async def search(self, text, secs, limit):
        i = str(uuid.uuid4())
        d = await self.req(
            "POST",
            "/searches",
            json={
                "id": i,
                "searchText": text,
                "fileLimit": 10000,
                "filterResponses": True,
                "maximumPeerQueueLength": 1000000,
                "minimumPeerUploadSpeed": 0,
                "minimumResponseFileCount": 1,
                "responseLimit": limit,
                "searchTimeout": secs * 1000,
            },
        )
        i = str(d.get("id") or i) if isinstance(d, dict) else i
        log.info("search_submitted id=%s query=%r", i, text)
        return i

    async def state(self, i):
        return await self.req("GET", f"/searches/{i}")

    async def responses(self, i):
        d = await self.req("GET", f"/searches/{i}/responses")
        return (
            d
            if isinstance(d, list)
            else d.get("responses", [])
            if isinstance(d, dict)
            else []
        )

    async def enqueue(self, u, files):
        p = [{"filename": f["filename"], "size": int(f["size"])} for f in files]
        log.info(
            "enqueue user=%s files=%d bytes=%d", u, len(p), sum(x["size"] for x in p)
        )
        return await self.req("POST", f"/transfers/downloads/{u}", json=p)

    async def downloads(self, username=None):
        data = await self.req(
            "GET",
            "/transfers/downloads",
        )

        if not data:
            return []

        if username is None:
            return data

        target_username = username.casefold()

        if isinstance(data, list):
            return [
                transfer
                for transfer in data
                if (
                    not isinstance(transfer, dict)
                    or str(transfer.get("username", "")).casefold() == target_username
                )
            ]

        if isinstance(data, dict):
            if isinstance(data.get("transfers"), list):
                filtered = [
                    transfer
                    for transfer in data["transfers"]
                    if (
                        isinstance(transfer, dict)
                        and str(transfer.get("username", "")).casefold()
                        == target_username
                    )
                ]

                return {
                    **data,
                    "transfers": filtered,
                }

            if str(data.get("username", "")).casefold() == target_username:
                return data

        return []
