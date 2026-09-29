import logging
from urllib.parse import quote
import httpx
log = logging.getLogger("sas.abr")
class AbrClient:
    def __init__(self, settings):
        self.s=settings; self.base=settings.abr_url.rstrip("/")
        self.c=httpx.AsyncClient(headers={"Authorization":f"Bearer {settings.abr_api_key}","Accept":"application/json"},timeout=settings.abr_timeout_seconds,verify=settings.abr_verify_ssl)
    async def close(self): await self.c.aclose()
    async def request(self,method,path):
        r=await self.c.request(method,self.base+path); r.raise_for_status()
        if not r.content:return None
        return r.json() if "json" in r.headers.get("content-type","") else r.text
    @staticmethod
    def items(payload):
        if isinstance(payload,list):return payload
        if isinstance(payload,dict) and isinstance(payload.get("value"),list):return payload["value"]
        return []
    async def standard_requests(self):return self.items(await self.request("GET","/api/requests"))
    async def manual_requests(self):return self.items(await self.request("GET","/api/requests/manual"))
    async def mark_standard_downloaded(self,value):
        await self.request("PATCH",f"/api/requests/{quote(str(value),safe='')}/downloaded");log.info("abr_marked_downloaded type=standard id=%s",value)
    async def mark_manual_downloaded(self,value):
        await self.request("PATCH",f"/api/requests/manual/{quote(str(value),safe='')}/downloaded");log.info("abr_marked_downloaded type=manual id=%s",value)
