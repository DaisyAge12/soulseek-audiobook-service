import asyncio
import json
import logging
import re
import time

from app.abr import AbrClient
from app.duplicate import DuplicateDetector
from app.mover import CompletedDownloadMover
from app.schemas import Candidate, RequestIn
from app.scoring import group, preferred_format_name, rank

log = logging.getLogger("sas.worker")
FAILURE_STATES = {
    "cancelled", "canceled", "errored", "error", "failed", "rejected",
    "timedout", "timed out", "aborted",
}


def normalized_state(state):
    return str(state or "").strip().casefold()


def is_success_state(state):
    value = normalized_state(state)
    return "completed" in value or "succeeded" in value


def is_failure_state(state):
    value = normalized_state(state)
    return any(failure in value for failure in FAILURE_STATES)


def flatten_transfers(value):
    if isinstance(value, list):
        for item in value:
            yield from flatten_transfers(item)
    elif isinstance(value, dict):
        if (("filename" in value or "fileName" in value)
                and ("state" in value or "percentComplete" in value)):
            yield value
        for key in ("directories", "files", "transfers"):
            if key in value:
                yield from flatten_transfers(value[key])


def file_key(filename):
    return filename.replace("/", chr(92)).casefold()


def compact_spaces(value):
    return " ".join(value.split())


def punctuation_relaxed(value):
    return compact_spaces(re.sub(r"[^A-Za-z0-9]+", " ", value))


def build_search_queries(request, settings):
    author = compact_spaces(request.author)
    title = compact_spaces(request.title)
    queries = [f"{author} {title}".strip()]
    relaxed = punctuation_relaxed(f"{author} {title}")
    if relaxed and relaxed.casefold() != queries[0].casefold():
        queries.append(relaxed)
    if settings.search_include_audiobook_variant:
        queries.append(f"{author} {title} audiobook".strip())
        if relaxed:
            queries.append(f"{relaxed} audiobook")
    if settings.search_include_title_only_variant:
        queries.append(title)
        if settings.search_include_audiobook_variant:
            queries.append(f"{title} audiobook")
    unique, seen = [], set()
    for query in queries:
        normalized = query.casefold()
        if query and normalized not in seen:
            unique.append(query)
            seen.add(normalized)
    return unique


def candidate_identity(candidate):
    return (
        candidate.username.casefold(),
        candidate.directory.replace("/", chr(92)).casefold(),
        tuple(sorted(file_key(item.filename) for item in candidate.required_files)),
    )


class Worker:
    def __init__(self, db, client, settings):
        self.db = db
        self.c = client
        self.s = settings
        self.stop = False
        self.mover = CompletedDownloadMover(settings)
        self.duplicates = DuplicateDetector(settings, db)
        self.abr = AbrClient(settings)

    async def run(self):
        log.info("worker_started")
        while not self.stop:
            job = self.db.next()
            if job:
                await self.process(job)
            else:
                await asyncio.sleep(self.s.worker_poll_seconds)

    async def run_search(self, job_id, query):
        search_id = await self.c.search(query, self.s.search_timeout_seconds, self.s.search_response_limit)
        deadline = time.monotonic() + self.s.search_timeout_seconds + 10
        state = {}
        while time.monotonic() < deadline:
            state = await self.c.state(search_id) or {}
            if state.get("isComplete") or str(state.get("state", "")).lower() in {"completed", "complete"}:
                break
            await asyncio.sleep(1)
        responses = state.get("responses") or await self.c.responses(search_id)
        log.info("search_complete job=%s search_id=%s responses=%d query=%r", job_id, search_id, len(responses), query)
        return responses

    async def collect_candidates(self, job_id, request):
        queries = build_search_queries(request, self.s)
        pooled = {}
        log.info("search_plan job=%s queries=%d values=%r", job_id, len(queries), queries)
        for query_number, query in enumerate(queries, start=1):
            responses = await self.run_search(job_id, query)
            query_ranked = rank(group(responses), request)
            query_best = query_ranked[: self.s.search_candidates_per_query]
            log.info("search_candidates job=%s query=%d/%d grouped=%d retained=%d", job_id, query_number, len(queries), len(query_ranked), len(query_best))
            for candidate in query_best:
                identity = candidate_identity(candidate)
                existing = pooled.get(identity)
                if existing is None or candidate.score > existing.score:
                    pooled[identity] = candidate
        return rank(list(pooled.values()), request)

    def selected_candidate(self, job, candidates):
        number = max(1, int(job.get("current_candidate") or 1))
        if number > len(candidates):
            raise RuntimeError(f"Stored candidate {number} does not exist")
        return number, candidates[number - 1]

    async def mark_abr_downloaded(self, job_id, request):
        if not self.s.abr_enabled or not self.s.abr_auto_mark_downloaded or not request.source_id:
            return
        try:
            if request.source_type == "abr_manual": await self.abr.mark_manual_downloaded(request.source_id)
            elif request.source_type == "abr_standard": await self.abr.mark_standard_downloaded(request.source_id)
        except Exception as error:
            log.exception("abr_completion_callback_failed job=%s error=%s", job_id, error)
            if self.s.abr_callback_failure_fatal: raise
            self.db.update(job_id, error=f"ABR completion callback failed: {error}")

    async def finish(self, job_id, request, candidate):
        if self.s.enable_post_download_move:
            self.db.update(job_id, status="moving", progress=100, error=None)
            await asyncio.to_thread(self.mover.move, job_id, request, candidate)
        await self.mark_abr_downloaded(job_id, request)
        self.db.update(job_id, status="completed", progress=100, error=None)
        log.info("job_completed job=%s", job_id)

    async def process(self, job):
        job_id = job["id"]
        request = RequestIn(**json.loads(job["request_json"]))
        stored = json.loads(job.get("candidates_json") or "[]")

        try:
            if job["status"] == "moving":
                candidates = [Candidate(**item) for item in stored]
                _, candidate = self.selected_candidate(job, candidates)
                log.info("move_recovery_started job=%s", job_id)
                await self.finish(job_id, request, candidate)
                return

            if job["status"] == "received" and self.s.duplicate_detection_enabled:
                duplicate = self.duplicates.detect(request, exclude_job_id=job_id)
                if duplicate:
                    self.db.update(
                        job_id,
                        status="duplicate",
                        progress=100,
                        error=json.dumps(duplicate),
                    )
                    await self.mark_abr_downloaded(job_id, request)
                    log.info("job_completed_as_duplicate job=%s source=%s", job_id, duplicate["source"])
                    return

            recovering = job["status"] == "recovering"
            if recovering and stored:
                ranked_candidates = [Candidate(**item) for item in stored]
                log.info("job_recovery_candidates job=%s count=%d", job_id, len(ranked_candidates))
            else:
                self.db.update(job_id, status="searching", error=None)
                ranked_candidates = await self.collect_candidates(job_id, request)
                self.db.update(job_id, status="scoring", candidates_json=json.dumps([c.model_dump() for c in ranked_candidates]))

            candidates = [c for c in ranked_candidates if c.score >= self.s.min_candidate_score][: self.s.max_candidates]
            for position, candidate in enumerate(ranked_candidates[:20], start=1):
                log.info("candidate_ranked job=%s rank=%d score=%.2f format=%s user=%s files=%d directory=%r", job_id, position, candidate.score, preferred_format_name(candidate), candidate.username, len(candidate.required_files), candidate.directory)
            log.info("candidates_selected job=%s ranked=%d accepted=%d minimum_score=%.2f", job_id, len(ranked_candidates), len(candidates), self.s.min_candidate_score)
            if not candidates:
                raise RuntimeError("No candidate met minimum score")

            start_candidate = max(1, int(job.get("current_candidate") or 1)) if recovering else 1
            for candidate_number, candidate in enumerate(candidates, start=1):
                if candidate_number < start_candidate:
                    continue
                self.db.update(job_id, current_candidate=candidate_number, status="queued")
                start_attempt = max(1, int(job.get("current_attempt") or 1)) if recovering and candidate_number == start_candidate else 1
                for attempt_number in range(start_attempt, self.s.max_attempts_per_candidate + 1):
                    self.db.update(job_id, current_attempt=attempt_number, status="queued")
                    log.info("candidate_attempt job=%s candidate=%d attempt=%d score=%.2f format=%s user=%s directory=%r recovering=%s", job_id, candidate_number, attempt_number, candidate.score, preferred_format_name(candidate), candidate.username, candidate.directory, recovering)
                    try:
                        await self.attempt(job_id, candidate, recovery_mode=recovering)
                        await self.finish(job_id, request, candidate)
                        return
                    except Exception as error:
                        log.warning("attempt_failed job=%s candidate=%d attempt=%d error=%s", job_id, candidate_number, attempt_number, error)
                        self.db.update(job_id, status="retrying", error=str(error))
                        recovering = False
                        if attempt_number < self.s.max_attempts_per_candidate:
                            await asyncio.sleep(min(30, attempt_number * 5))
                self.db.update(job_id, status="falling_back", error=f"Candidate {candidate_number} exhausted")
            raise RuntimeError("All candidates exhausted")
        except Exception as error:
            log.exception("job_failed job=%s", job_id)
            self.db.update(job_id, status="failed", error=str(error))

    async def attempt(self, job_id, candidate, recovery_mode=False):
        expected = {file_key(item.filename): item for item in candidate.required_files}
        existing_response = await self.c.downloads(candidate.username)
        existing = {file_key(item.get("filename") or item.get("fileName", "")): item for item in flatten_transfers(existing_response)}
        failed_existing = [existing[key] for key in expected if key in existing and is_failure_state(existing[key].get("state"))]
        if failed_existing:
            raise RuntimeError(f"slskd reports transfer as {failed_existing[0].get('state')}")
        completed = {key for key in expected if key in existing and is_success_state(existing[key].get("state"))}
        active = {key for key in expected if key in existing and not is_success_state(existing[key].get("state")) and not is_failure_state(existing[key].get("state"))}
        if recovery_mode and not active and len(completed) != len(expected):
            raise RuntimeError("Recovered job has no active slskd transfers; treating it as cancelled or removed")
        files_to_enqueue = [item.model_dump() for item in candidate.files if file_key(item.filename) not in completed and file_key(item.filename) not in active]
        if files_to_enqueue:
            log.info("enqueue_candidate job=%s user=%s format=%s required=%d optional=%d enqueue=%d", job_id, candidate.username, preferred_format_name(candidate), len(candidate.required_files), len(candidate.optional_files), len(files_to_enqueue))
            await self.c.enqueue(candidate.username, files_to_enqueue)

        started = last_change = time.monotonic()
        missing_since = None
        last_bytes = -1
        last_bucket = -1
        while time.monotonic() - started < self.s.candidate_timeout_seconds:
            response = await self.c.downloads(candidate.username)
            transfers = {file_key(item.get("filename") or item.get("fileName", "")): item for item in flatten_transfers(response)}
            failed = [transfers[key] for key in expected if key in transfers and is_failure_state(transfers[key].get("state"))]
            if failed:
                raise RuntimeError(f"Required transfer failed: {failed[0].get('state')}")
            completed_keys = [key for key in expected if key in transfers and is_success_state(transfers[key].get("state"))]
            if len(completed_keys) == len(expected):
                log.info("download_progress job=%s progress=100.0 files=%d/%d", job_id, len(completed_keys), len(expected))
                return
            visible = [key for key in expected if key in transfers]
            if not visible:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > self.s.missing_transfer_grace_seconds:
                    raise RuntimeError("All required transfers disappeared from slskd; download was likely cancelled or removed")
            else:
                missing_since = None
            transferred = sum(int(transfers[key].get("bytesTransferred") or 0) for key in expected if key in transfers)
            if transferred != last_bytes:
                last_bytes = transferred
                last_change = time.monotonic()
            if time.monotonic() - last_change > self.s.stall_timeout_seconds:
                raise RuntimeError(f"Transfer stalled at {transferred} bytes")
            total = sum(item.size for item in expected.values())
            progress = round(min(99, 100 * transferred / total), 2) if total else 0
            self.db.update(job_id, status="downloading", progress=progress)
            bucket = int(progress // 5) * 5
            if bucket > last_bucket:
                log.info("download_progress job=%s progress=%.1f files=%d/%d bytes=%d/%d", job_id, progress, len(completed_keys), len(expected), transferred, total)
                last_bucket = bucket
            await asyncio.sleep(self.s.transfer_poll_seconds)
        raise RuntimeError("Candidate timeout exceeded")
