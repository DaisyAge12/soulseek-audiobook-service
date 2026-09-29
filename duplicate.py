import logging
from pathlib import Path

from app.mover import safe_component

log = logging.getLogger("sas.duplicate")
AUDIO_EXTENSIONS = {".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav"}


class DuplicateDetector:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.completed_root = Path(settings.completed_audiobooks_root)

    def destination(self, request):
        return (
            self.completed_root
            / safe_component(request.author)
            / safe_component(request.title)
        )

    def filesystem_duplicate(self, request):
        destination = self.destination(request)
        if not destination.is_dir():
            return None

        audio_files = [
            path
            for path in destination.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
        ]
        if not audio_files:
            return None

        readable_files = []
        total_bytes = 0
        for path in audio_files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > 0:
                readable_files.append(path)
                total_bytes += size

        if not readable_files:
            return None

        return {
            "source": "filesystem",
            "path": str(destination),
            "audio_file_count": len(readable_files),
            "total_audio_bytes": total_bytes,
        }

    def database_duplicate(self, request, exclude_job_id=None):
        match = self.db.find_completed_request(
            request.title,
            request.author,
            exclude_job_id=exclude_job_id,
        )
        if not match:
            return None
        return {
            "source": "sas_database",
            "job_id": match["id"],
            "external_request_id": match["external_request_id"],
        }

    def detect(self, request, exclude_job_id=None):
        filesystem = self.filesystem_duplicate(request)
        if filesystem:
            log.info(
                "duplicate_detected source=filesystem title=%r author=%r path=%s",
                request.title,
                request.author,
                filesystem["path"],
            )
            return filesystem

        database = self.database_duplicate(request, exclude_job_id)
        if database:
            log.info(
                "duplicate_detected source=database title=%r author=%r previous_job=%s",
                request.title,
                request.author,
                database["job_id"],
            )
            return database

        return None
