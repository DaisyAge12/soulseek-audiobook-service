import logging
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path

from app.schemas import Candidate, RequestIn

log = logging.getLogger("sas.mover")
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MoveError(RuntimeError):
    pass


def safe_component(value):
    cleaned = INVALID_PATH_CHARS.sub("_", value).strip().rstrip(".")
    cleaned = " ".join(cleaned.split())
    return cleaned or "Unknown"


def normalized_parts(value):
    return [part.casefold() for part in value.replace("/", chr(92)).split(chr(92)) if part]


class CompletedDownloadMover:
    def __init__(self, settings):
        self.settings = settings
        self.download_root = Path(settings.download_root)
        self.completed_root = Path(settings.completed_audiobooks_root)

    def destination(self, request):
        return (
            self.completed_root
            / safe_component(request.author)
            / safe_component(request.title)
        )

    def _build_index(self):
        by_name_size = defaultdict(list)
        all_files = []
        if not self.download_root.exists():
            raise MoveError(f"Download root does not exist: {self.download_root}")

        for path in self.download_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            all_files.append((path, size))
            by_name_size[(path.name.casefold(), size)].append(path)
        return all_files, by_name_size

    def _resolve(self, remote_file, all_files, by_name_size):
        expected_size = int(remote_file.size)
        remote_parts = normalized_parts(remote_file.filename)

        direct = self.download_root.joinpath(*remote_parts)
        if direct.is_file() and direct.stat().st_size == expected_size:
            return direct

        candidates = by_name_size.get((Path(remote_file.filename).name.casefold(), expected_size), [])
        if not candidates:
            # Windows remote paths are not parsed as filenames on Linux.
            remote_name = remote_parts[-1] if remote_parts else ""
            candidates = by_name_size.get((remote_name, expected_size), [])

        if len(candidates) == 1:
            return candidates[0]

        if len(candidates) > 1:
            suffix_matches = []
            for candidate in candidates:
                try:
                    relative_parts = [part.casefold() for part in candidate.relative_to(self.download_root).parts]
                except ValueError:
                    continue
                compare_length = min(len(remote_parts), len(relative_parts))
                if compare_length and relative_parts[-compare_length:] == remote_parts[-compare_length:]:
                    suffix_matches.append(candidate)
            if len(suffix_matches) == 1:
                return suffix_matches[0]
            raise MoveError(
                f"Multiple local files match remote file {remote_file.filename!r} "
                f"with size {expected_size}"
            )

        raise MoveError(
            f"Completed file was not found beneath {self.download_root}: "
            f"{remote_file.filename!r} ({expected_size} bytes)"
        )

    def _move_one(self, source, destination, expected_size, policy, job_id):
        destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.exists():
            destination_size = destination.stat().st_size
            if destination_size == expected_size:
                if source.exists() and source.resolve() != destination.resolve():
                    source.unlink()
                log.info(
                    "move_duplicate_skipped job=%s destination=%s bytes=%d",
                    job_id,
                    destination,
                    expected_size,
                )
                return "existing"

            if policy == "overwrite":
                destination.unlink()
            elif policy == "version":
                stem = destination.stem
                suffix = destination.suffix
                number = 2
                while destination.exists():
                    destination = destination.with_name(f"{stem} ({number}){suffix}")
                    number += 1
            else:
                raise MoveError(
                    f"Destination conflict: {destination} exists with a different size"
                )

        shutil.move(str(source), str(destination))
        if not destination.is_file() or destination.stat().st_size != expected_size:
            raise MoveError(f"Move verification failed for {destination}")

        log.info(
            "move_file job=%s source=%s destination=%s bytes=%d",
            job_id,
            source,
            destination,
            expected_size,
        )
        return "moved"

    def move(self, job_id, request: RequestIn, candidate: Candidate):
        if not self.settings.enable_post_download_move:
            return None

        policy = self.settings.duplicate_policy.casefold()
        if policy not in {"skip", "overwrite", "version"}:
            raise MoveError(f"Unsupported duplicate policy: {policy}")

        destination_root = self.destination(request)
        destination_root.mkdir(parents=True, exist_ok=True)
        log.info(
            "move_started job=%s destination=%s required=%d optional=%d",
            job_id,
            destination_root,
            len(candidate.required_files),
            len(candidate.optional_files) if self.settings.move_optional_files else 0,
        )

        all_files, by_name_size = self._build_index()
        selected = list(candidate.required_files)
        if self.settings.move_optional_files:
            selected.extend(candidate.optional_files)

        moved = 0
        existing = 0
        total_bytes = 0
        used_destinations = set()

        for remote_file in selected:
            expected_size = int(remote_file.size)
            source = self._resolve(remote_file, all_files, by_name_size)
            destination = destination_root / source.name

            # Preserve duplicate basenames instead of silently overwriting one.
            if destination.name.casefold() in used_destinations:
                destination = destination_root / f"{source.stem} ({expected_size}){source.suffix}"
            used_destinations.add(destination.name.casefold())

            result = self._move_one(source, destination, expected_size, policy, job_id)
            moved += result == "moved"
            existing += result == "existing"
            total_bytes += expected_size

        log.info(
            "move_completed job=%s destination=%s moved=%d existing=%d bytes=%d",
            job_id,
            destination_root,
            moved,
            existing,
            total_bytes,
        )
        return str(destination_root)
