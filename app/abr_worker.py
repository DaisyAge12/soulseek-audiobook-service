import asyncio
import json
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
    @staticmethod
    def standard_id(x):
        b=x.get("book") if isinstance(x,dict) else None
        if not b or b.get("downloaded"):return None
        asin=str(b.get("asin") or "").strip()
        return f"abr:asin:{asin}" if asin else None
    @staticmethod
    def manual_id(x):
        if not isinstance(x,dict) or x.get("downloaded"):return None
        rid=str(x.get("id") or "").strip()
        return f"abr:manual:{rid}" if rid else None
    async def poll_once(self):
        n=0;active={}
        sources=[
            ("abr_standard",self.s.abr_import_standard_requests,self.abr.standard_requests,self.import_standard,self.standard_id),
            ("abr_manual",self.s.abr_import_manual_requests,self.abr.manual_requests,self.import_manual,self.manual_id),
        ]
        for source_type,enabled,fetch,handler,ident in sources:
            if not enabled:continue
            try:items=await fetch()
            except Exception:log.exception("abr_fetch_failed source=%s",source_type);continue
            pending=set()
            for x in items:
                try:
                    eid=ident(x)
                    if eid:pending.add(eid)
                    n+=handler(x)
                except Exception:log.exception("abr_item_import_failed source=%s item=%r",source_type,x)
            active[source_type]=pending
        self.close_stale_no_match(active)
        log.info("abr_poll_complete imported=%d",n)
    def close_stale_no_match(self,active):
        # A no_match job whose ABR request is gone or already downloaded should stop retrying.
        for job in self.db.list_status("no_match"):
            try:st=json.loads(job["request_json"]).get("source_type")
            except (TypeError,ValueError):continue
            pending=active.get(st)
            if pending is None or job["external_request_id"] in pending:continue
            self.db.update(job["id"],status="cancelled",error="Request no longer pending in ABR",next_retry_at=None)
            log.info("no_match_job_cancelled job=%s external=%s",job["id"],job["external_request_id"])
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
