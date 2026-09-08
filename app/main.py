import asyncio
import json
import logging
import logging.handlers
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from app.abr import AbrClient
from app.abr_worker import AbrPollWorker
from app.config import Settings
from app.database import DB
from app.duplicate import DuplicateDetector
from app.schemas import Candidate, RequestIn
from app.scoring import preferred_format_name
from app.slskd import Client
from app.worker import Worker

settings = Settings()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
root_logger = logging.getLogger()
root_logger.setLevel(settings.log_level.upper())

if not root_logger.handlers:
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    try:
        Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.log_file,
            maxBytes=10_000_000,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except OSError:
        root_logger.exception("file_logging_unavailable")

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

log = logging.getLogger("sas.api")
db = DB(settings.database_path)
db.init()
client = Client(settings)
worker = Worker(db, client, settings)
abr_client = AbrClient(settings)
abr_worker = AbrPollWorker(db, abr_client, settings)
duplicates = DuplicateDetector(settings, db)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(worker.run())
    abr_task = asyncio.create_task(abr_worker.run())
    yield
    worker.stop = True
    abr_worker.stop = True
    task.cancel()
    abr_task.cancel()
    await client.close()
    await abr_client.close()


app = FastAPI(
    title="Soulseek Audiobook Service",
    version="0.9.0",
    lifespan=lifespan,
)


def auth(x_api_key: str = Header(alias="X-API-Key")):
    if not secrets.compare_digest(x_api_key, settings.sas_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
async def health():
    try:
        await client.health()
        slskd_status = "reachable"
    except Exception as error:
        slskd_status = "unreachable"
        log.warning("slskd_health_failed error=%s", error)
    return {"status": "healthy", "slskd": slskd_status, "version": "0.9.0"}


@app.post(
    "/v1/requests",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(auth)],
)
def create_request(request: RequestIn):
    existing = db.external(request.external_request_id)
    if existing:
        return {
            "job_id": existing["id"],
            "external_request_id": existing["external_request_id"],
            "status": existing["status"],
            "duplicate": True,
        }

    if settings.duplicate_detection_enabled:
        duplicate = duplicates.detect(request)
        if duplicate:
            policy = settings.duplicate_policy_on_request.casefold()
            if policy == "reject":
                raise HTTPException(
                    status_code=409,
                    detail={"message": "Audiobook already exists", "duplicate": duplicate},
                )
            if policy != "complete":
                raise HTTPException(
                    status_code=500,
                    detail="DUPLICATE_POLICY_ON_REQUEST must be complete or reject",
                )

            job_id = str(uuid4())
            db.create(job_id, request.external_request_id, request.model_dump())
            db.update(
                job_id,
                status="duplicate",
                progress=100,
                error=json.dumps(duplicate),
            )
            log.info(
                "request_completed_as_duplicate job=%s title=%r author=%r source=%s",
                job_id,
                request.title,
                request.author,
                duplicate["source"],
            )
            return {
                "job_id": job_id,
                "external_request_id": request.external_request_id,
                "status": "duplicate",
                "duplicate": duplicate,
            }

    job_id = str(uuid4())
    db.create(job_id, request.external_request_id, request.model_dump())
    log.info(
        "request_accepted job=%s external=%s title=%r author=%r",
        job_id,
        request.external_request_id,
        request.title,
        request.author,
    )
    return {
        "job_id": job_id,
        "external_request_id": request.external_request_id,
        "status": "received",
    }


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def get_job(job_id: str):
    job = db.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    duplicate_detail = None
    if job["status"] == "duplicate" and job.get("error"):
        try:
            duplicate_detail = json.loads(job["error"])
        except (TypeError, json.JSONDecodeError):
            duplicate_detail = None

    return {
        "job_id": job["id"],
        "external_request_id": job["external_request_id"],
        "status": job["status"],
        "current_candidate": job["current_candidate"],
        "current_attempt": job["current_attempt"],
        "progress": job["progress"],
        "error": None if duplicate_detail else job["error"],
        "duplicate": duplicate_detail,
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@app.get("/v1/jobs/{job_id}/candidates", dependencies=[Depends(auth)])
def get_candidates(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    include_rejected: bool = Query(default=True),
):
    job = db.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    raw_candidates = json.loads(job.get("candidates_json") or "[]")
    output = []
    for index, candidate in enumerate(raw_candidates, start=1):
        score = float(candidate.get("score") or 0)
        if not include_rejected and score < settings.min_candidate_score:
            continue
        model = Candidate(**candidate)
        required_files = candidate.get("required_files", [])
        optional_files = candidate.get("optional_files", [])
        output.append({
            "rank": index,
            "accepted": score >= settings.min_candidate_score,
            "score": score,
            "format": preferred_format_name(model),
            "username": candidate.get("username"),
            "directory": candidate.get("directory"),
            "required_file_count": len(required_files),
            "optional_file_count": len(optional_files),
            "total_audio_bytes": sum(int(item.get("size") or 0) for item in required_files),
            "has_free_slot": bool(candidate.get("has_free_slot")),
            "queue_length": int(candidate.get("queue_length") or 0),
            "upload_speed": int(candidate.get("upload_speed") or 0),
            "reasons": candidate.get("reasons", []),
            "files": required_files,
        })
        if len(output) >= limit:
            break

    return {
        "job_id": job_id,
        "minimum_score": settings.min_candidate_score,
        "count": len(output),
        "candidates": output,
    }
