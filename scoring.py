import os
import re
from collections import defaultdict
from difflib import SequenceMatcher

from app.schemas import Candidate, FileItem

# Format is intentionally a major ranking signal for an audiobook service.
FORMAT_BONUS = {
    ".m4b": 30,
    ".mp3": 10,
    ".m4a": 8,
    ".ogg": 6,
    ".opus": 5,
    ".aac": 4,
    ".flac": 3,
    ".wav": 2,
}

FORMAT_ORDER = {
    extension: position
    for position, extension in enumerate(
        [".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav"],
        start=1,
    )
}

AUDIO_EXTENSIONS = set(FORMAT_BONUS)
OPTIONAL_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".opf",
    ".nfo",
    ".txt",
    ".cue",
}


def norm(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def sim(left, right):
    return SequenceMatcher(None, norm(left), norm(right)).ratio()


def candidate_extension(candidate):
    extensions = {
        item.extension.lower()
        for item in candidate.required_files
        if item.extension
    }

    if len(extensions) == 1:
        return next(iter(extensions))

    # Candidates are normally separated by extension in group().
    # Keep this fallback deterministic for old stored jobs.
    return min(
        extensions,
        key=lambda extension: FORMAT_ORDER.get(extension, 999),
        default="",
    )


def preferred_format(candidate):
    extension = candidate_extension(candidate)
    return FORMAT_BONUS.get(extension, 0)


def preferred_format_name(candidate):
    return candidate_extension(candidate).lstrip(".") or "unknown"


def group(responses):
    # First collect every file by peer and remote directory.
    grouped = defaultdict(
        lambda: {
            "audio_by_extension": defaultdict(list),
            "optional": [],
            "slot": False,
            "queue": 0,
            "speed": 0,
        }
    )

    for response in responses:
        username = response.get("username", "")

        for raw_file in response.get("files", []):
            filename = (
                raw_file.get("filename")
                or raw_file.get("name")
                or raw_file.get("fullName")
            )

            if not username or not filename:
                continue

            filename = filename.replace("/", chr(92))
            directory = (
                filename.rsplit(chr(92), 1)[0]
                if chr(92) in filename
                else ""
            )
            extension = os.path.splitext(filename)[1].lower()
            size = int(raw_file.get("size") or raw_file.get("length") or 0)

            if size <= 0:
                continue

            item = FileItem(
                filename=filename,
                size=size,
                extension=extension,
                bitrate=raw_file.get("bitRate"),
                length=raw_file.get("length"),
            )

            entry = grouped[(username, directory)]
            entry["slot"] = bool(response.get("hasFreeUploadSlot"))
            entry["queue"] = int(response.get("queueLength") or 0)
            entry["speed"] = int(response.get("uploadSpeed") or 0)

            if extension in AUDIO_EXTENSIONS:
                entry["audio_by_extension"][extension].append(item)
            elif extension in OPTIONAL_EXTENSIONS:
                entry["optional"].append(item)

    candidates = []

    # A directory containing both a full M4B and MP3 chapter files becomes
    # two candidates. SAS can therefore choose and enqueue only the M4B files,
    # rather than downloading duplicate representations of the same book.
    for (username, directory), entry in grouped.items():
        for extension, audio_files in entry["audio_by_extension"].items():
            if not audio_files:
                continue

            optional_files = list(entry["optional"])

            candidates.append(
                Candidate(
                    username=username,
                    directory=directory,
                    files=audio_files + optional_files,
                    required_files=audio_files,
                    optional_files=optional_files,
                    has_free_slot=entry["slot"],
                    queue_length=entry["queue"],
                    upload_speed=entry["speed"],
                )
            )

    return candidates


def rank(candidates, request):
    ranked = []

    for candidate in candidates:
        search_text = norm(
            candidate.directory
            + " "
            + " ".join(item.filename for item in candidate.required_files)
        )

        title_sequence = sim(request.title, search_text)
        author_sequence = sim(request.author, search_text)

        title_tokens = {
            token for token in norm(request.title).split() if len(token) >= 2
        }
        author_tokens = {
            token for token in norm(request.author).split() if len(token) >= 2
        }
        result_tokens = set(search_text.split())

        title_token_ratio = (
            len(title_tokens & result_tokens) / len(title_tokens)
            if title_tokens
            else 0
        )
        author_token_ratio = (
            len(author_tokens & result_tokens) / len(author_tokens)
            if author_tokens
            else 0
        )

        title_match = max(title_sequence, title_token_ratio)
        author_match = max(author_sequence, author_token_ratio)

        candidate.reasons = [
            f"title={title_match:.2f}",
            f"author={author_match:.2f}",
            f"title_sequence={title_sequence:.2f}",
            f"author_sequence={author_sequence:.2f}",
            f"title_tokens={title_token_ratio:.2f}",
            f"author_tokens={author_token_ratio:.2f}",
        ]

        if title_match < 0.35:
            candidate.score = 0
            candidate.reasons.append("rejected:title-mismatch")
            ranked.append(candidate)
            continue

        if author_match < 0.25:
            candidate.score = 0
            candidate.reasons.append("rejected:author-mismatch")
            ranked.append(candidate)
            continue

        points = 35 * title_match + 25 * author_match
        extension = candidate_extension(candidate)
        format_bonus = FORMAT_BONUS.get(extension, 0)
        points += format_bonus
        candidate.reasons.append(
            f"format={extension.lstrip('.')}:bonus={format_bonus}"
        )

        file_count = len(candidate.required_files)

        if extension == ".m4b":
            if file_count == 1:
                points += 20
                candidate.reasons.append("single-m4b:+20")
            else:
                points += 18
                candidate.reasons.append(f"chaptered-m4b:{file_count}:+18")
        elif extension == ".mp3":
            if file_count > 1:
                points += 5
                candidate.reasons.append(f"multi-mp3:{file_count}:+5")
            else:
                candidate.reasons.append("single-mp3:+0")
        elif file_count > 1:
            points += 3
            candidate.reasons.append(f"multi-file:{file_count}:+3")

        total_audio_size = sum(item.size for item in candidate.required_files)
        if total_audio_size >= 50_000_000:
            points += 5
            candidate.reasons.append("size-sane:+5")

        if request.narrator and norm(request.narrator) in search_text:
            points += 5
            candidate.reasons.append("narrator:+5")

        if request.language and norm(request.language) in search_text:
            points += 3
            candidate.reasons.append("language:+3")

        # Availability is deliberately minor. It must not outweigh M4B quality.
        if candidate.has_free_slot:
            points += 2
            candidate.reasons.append("free-slot:+2")

        if candidate.upload_speed:
            speed_bonus = min(1, candidate.upload_speed / 5_000_000)
            points += speed_bonus
            candidate.reasons.append(f"speed:+{speed_bonus:.2f}")

        candidate.score = round(points, 2)
        ranked.append(candidate)

    return sorted(
        ranked,
        key=lambda candidate: (
            candidate.score,
            -FORMAT_ORDER.get(candidate_extension(candidate), 999),
            candidate.has_free_slot,
            candidate.upload_speed,
        ),
        reverse=True,
    )
