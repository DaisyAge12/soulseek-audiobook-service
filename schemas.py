from pydantic import BaseModel, Field


class RequestIn(BaseModel):
    external_request_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=500)
    narrator: str | None = Field(None, max_length=500)
    language: str | None = Field(None, max_length=100)
    asin: str | None = None
    subtitle: str | None = None
    authors: list[str] = []
    narrators: list[str] = []
    runtime_length_min: int | None = None
    release_date: str | None = None
    source_type: str | None = None
    source_id: str | None = None


class FileItem(BaseModel):
    filename: str
    size: int
    extension: str = ""
    bitrate: int | None = None
    length: int | None = None


class Candidate(BaseModel):
    username: str
    directory: str
    files: list[FileItem]
    required_files: list[FileItem]
    optional_files: list[FileItem] = []
    has_free_slot: bool = False
    queue_length: int = 0
    upload_speed: int = 0
    score: float = 0
    reasons: list[str] = []
