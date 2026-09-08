from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    sas_api_key: str = Field(min_length=16)
    slskd_url: str
    slskd_api_key: str
    slskd_url_base: str = ""
    slskd_verify_ssl: bool = True

    database_path: str = "/data/sas.db"
    log_file: str = "/logs/sas.log"
    download_root: str = "/downloads"

    enable_post_download_move: bool = False
    completed_audiobooks_root: str = "/completed-audiobooks"
    move_optional_files: bool = True
    duplicate_policy: str = "skip"

    duplicate_detection_enabled: bool = True
    duplicate_policy_on_request: str = "complete"

    max_attempts_per_candidate: int = 3
    max_candidates: int = 5
    min_candidate_score: float = 25

    transfer_poll_seconds: int = 10
    stall_timeout_seconds: int = 900
    candidate_timeout_seconds: int = 21600
    missing_transfer_grace_seconds: int = 45

    search_timeout_seconds: int = 45
    search_response_limit: int = 100
    search_candidates_per_query: int = 5
    search_include_audiobook_variant: bool = True
    search_include_title_only_variant: bool = True

    abr_enabled: bool = False
    abr_url: str = ""
    abr_api_key: str = ""
    abr_poll_interval_seconds: int = 60
    abr_timeout_seconds: int = 30
    abr_verify_ssl: bool = True
    abr_import_standard_requests: bool = True
    abr_import_manual_requests: bool = True
    abr_auto_mark_downloaded: bool = True
    abr_callback_failure_fatal: bool = False

    worker_poll_seconds: int = 2
    log_level: str = "INFO"
