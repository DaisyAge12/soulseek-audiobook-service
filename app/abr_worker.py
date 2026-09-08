import asyncio
import logging
from app.schemas import RequestIn
log=logging.getLogger("sas.abr_worker")
class AbrPollWorker:
    def __init__(self,db,abr,settings):self.db=db;self.abr=abr;self.s=settings;self.stop=False
    async def run(self):
        if not self.s.abr_enabled:log.info("abr_polling_disabled");return
        log.info("abr_poll_worker_started interval=%d",self.s.abr_poll_interval_seconds)
        while not self.stop:
            try:await self.poll_once()
            except asyncio.CancelledError:raise
            except Exception:log.exception("abr_poll_failed")
            await asyncio.sleep(self.s.abr_poll_interval_seconds)
    async def poll_once(self):
        n=0
        if self.s.abr_import_standard_requests:
            for x in await self.abr.standard_requests():n+=self.import_standard(x)
        if self.s.abr_import_manual_requests:
            for x in await self.abr.manual_requests():n+=self.import_manual(x)
        log.info("abr_poll_complete imported=%d",n)
    def create(self,r):
        if self.db.external(r.external_request_id):return 0
        jid=self.db.create_generated(r.external_request_id,r.model_dump());log.info("abr_request_imported job=%s external=%s title=%r author=%r",jid,r.external_request_id,r.title,r.author);return 1
    def import_standard(self,x):
        b=x.get("book",{}) if isinstance(x,dict) else {}
        if not b or b.get("downloaded"):return 0
        a=[str(v).strip() for v in b.get("authors",[]) if str(v).strip()];n=[str(v).strip() for v in b.get("narrators",[]) if str(v).strip()];asin=str(b.get("asin") or "").strip();title=str(b.get("title") or "").strip()
        if not asin or not title or not a:return 0
        return self.create(RequestIn(external_request_id=f"abr:asin:{asin}",title=title,author=a[0],narrator=n[0] if n else None,asin=asin,subtitle=b.get("subtitle"),authors=a,narrators=n,runtime_length_min=b.get("runtime_length_min"),release_date=b.get("release_date"),source_type="abr_standard",source_id=asin))
    def import_manual(self,x):
        if not isinstance(x,dict) or x.get("downloaded"):return 0
        rid=str(x.get("id") or "").strip();title=str(x.get("title") or "").strip();a=[str(v).strip() for v in x.get("authors",[]) if str(v).strip()];n=[str(v).strip() for v in x.get("narrators",[]) if str(v).strip()]
        if not rid or not title or not a:return 0
        return self.create(RequestIn(external_request_id=f"abr:manual:{rid}",title=title,author=a[0],narrator=n[0] if n else None,subtitle=x.get("subtitle"),authors=a,narrators=n,release_date=x.get("publish_date"),source_type="abr_manual",source_id=rid))
