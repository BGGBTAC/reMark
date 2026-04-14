"""Configuration loading and validation for reMark."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class RemarkableConfig(BaseModel):
    device_token_path: str = "~/.remark-bridge/device_token"
    sync_folders: list[str] = Field(default_factory=list)
    ignore_folders: list[str] = Field(default_factory=lambda: ["Trash", "Quick sheets"])
    response_folder: str = "Responses"


class VLMConfig(BaseModel):
    provider: Literal["anthropic", "openai"] = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    prompt_template: str = "default"


class GoogleVisionConfig(BaseModel):
    credentials_path: str = "~/.remark-bridge/gcloud-credentials.json"
    language_hints: list[str] = Field(default_factory=lambda: ["en", "de"])


_OCR_ENGINES = Literal["remarkable_builtin", "google_vision", "vlm", "tesseract"]


class OCRConfig(BaseModel):
    primary: _OCR_ENGINES = "remarkable_builtin"
    fallback: _OCR_ENGINES | Literal["none"] = "google_vision"
    vlm: VLMConfig = Field(default_factory=VLMConfig)
    google_vision: GoogleVisionConfig = Field(default_factory=GoogleVisionConfig)
    confidence_threshold: float = 0.7


class ActionConfig(BaseModel):
    action_colors: list[int] = Field(default_factory=lambda: [6])
    question_colors: list[int] = Field(default_factory=lambda: [5])
    highlight_colors: list[int] = Field(default_factory=lambda: [3])
    detect_from_text: bool = True


class ProcessingConfig(BaseModel):
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"
    extract_actions: bool = True
    extract_tags: bool = True
    generate_summary: bool = True
    actions: ActionConfig = Field(default_factory=ActionConfig)


class GitConfig(BaseModel):
    enabled: bool = True
    remote: str = "origin"
    branch: str = "main"
    auto_commit: bool = True
    auto_push: bool = True
    commit_message_template: str = "sync: {count} notes from reMarkable ({date})"


class ObsidianConfig(BaseModel):
    vault_path: str = "/home/user/obsidian-vault"
    folder_map: dict[str, str] = Field(default_factory=lambda: {"_default": "Inbox"})
    git: GitConfig = Field(default_factory=GitConfig)


class WebSocketConfig(BaseModel):
    reconnect_delay: int = 5
    max_reconnect_delay: int = 300
    ping_interval: int = 30


class SyncConfig(BaseModel):
    mode: Literal["realtime", "scheduled", "manual", "all"] = "all"
    schedule: str = "*/15 * * * *"
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    state_db: str = "~/.remark-bridge/sync_state.db"
    push_responses: bool = True
    response_format: Literal["pdf", "notebook"] = "pdf"
    after_date: str | None = None


class ResponseConfig(BaseModel):
    """Controls how Claude-generated responses are pushed back to the tablet."""
    format: Literal["pdf", "notebook"] = "pdf"
    auto_trigger: bool = True
    trigger_on_questions: bool = True
    trigger_on_actions: bool = False
    include_analysis: bool = True
    include_related_notes: bool = True
    response_folder: str = "Responses"


class OneNoteConfig(BaseModel):
    """OneNote vault — parallel target alongside Obsidian."""
    enabled: bool = False
    notebook_name: str = "reMark"          # OneNote notebook to use
    default_section: str = "Inbox"
    # Map reMarkable folder -> OneNote section. "_default" for fallback.
    folder_map: dict[str, str] = Field(default_factory=lambda: {"_default": "Inbox"})
    create_missing_sections: bool = True


class TeamsConfig(BaseModel):
    """Microsoft Teams integration (digest posts + meeting correlation)."""
    enabled: bool = False
    # Incoming Webhook URL of the target channel (simplest auth model)
    webhook_url: str = ""
    # Cadence for the digest: "daily" | "weekly" | "off"
    digest_cadence: str = "weekly"
    digest_hour: int = 9                   # hour of the day (UTC) when digest fires
    digest_weekday: int = 1                # 1 = Monday, for weekly
    meeting_correlation: bool = True       # link meeting notes to Outlook events


class MicrosoftConfig(BaseModel):
    """Microsoft Graph integration (Outlook Tasks + Calendar + OneNote + Teams)."""
    enabled: bool = False

    # Azure AD application registration
    # See https://learn.microsoft.com/en-us/azure/active-directory/develop/quickstart-register-app
    client_id: str = ""
    tenant: str = "common"  # "common" for personal + work, or a specific tenant ID

    # Where to cache the OAuth token between runs
    token_cache_path: str = "~/.remark-bridge/msal_cache.bin"

    # Microsoft To Do sync
    todo_enabled: bool = True
    todo_list_name: str = "reMark"      # name of the target task list
    todo_create_list: bool = True       # auto-create the list if missing

    # Outlook Calendar sync
    calendar_enabled: bool = False
    calendar_id: str = ""               # empty = default calendar

    # OneNote (v0.3+)
    onenote: OneNoteConfig = Field(default_factory=OneNoteConfig)

    # Teams (v0.3+)
    teams: TeamsConfig = Field(default_factory=TeamsConfig)


class SearchConfig(BaseModel):
    """Semantic search / RAG configuration."""
    enabled: bool = False
    backend: Literal["voyage", "openai", "local"] = "local"
    model: str = ""  # backend-specific default if empty
    api_key_env: str = ""  # for voyage/openai
    chunk_size: int = 512  # max chars per chunk
    chunk_overlap: int = 50
    top_k: int = 5
    min_score: float = 0.3
    synthesize_answer: bool = True
    synthesis_model: str = "claude-sonnet-4-20250514"


class ReverseSyncConfig(BaseModel):
    """Obsidian → reMarkable reverse sync — all three triggers are independent."""
    enabled: bool = False
    # Trigger A: any note with `push_to_tablet: true` in frontmatter
    trigger_on_frontmatter: bool = True
    # Trigger B: any note inside the configured folder (relative to vault)
    trigger_on_folder: bool = True
    folder: str = "To-Tablet"
    # Trigger C: on-demand queue via CLI / dashboard
    trigger_on_demand: bool = True

    # Output format and destination folder on the tablet
    format: str = "pdf"                     # "pdf" | "notebook"
    target_folder: str = "From-Vault"

    # After pushing, stamp the note with pushed_to_tablet_at timestamp
    stamp_frontmatter: bool = True


class PluginConfig(BaseModel):
    """Plugin system configuration."""
    enabled: bool = True
    # Directory scanned for plugin modules/packages
    plugin_dir: str = "~/.config/remark/plugins"
    # List of plugin names to disable (overrides auto-discovery)
    disabled: list[str] = Field(default_factory=list)
    # Extra named configs forwarded to individual plugins
    settings: dict[str, dict] = Field(default_factory=dict)


class WebConfig(BaseModel):
    """Web dashboard + PWA configuration."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    # Basic HTTP auth (set both to enable). Empty = no auth (localhost only!)
    username: str = ""
    password: str = ""
    # Branding / app name
    app_name: str = "reMark"
    # PWA: VAPID keys for Web Push (generate with `remark-bridge vapid-keys`)
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"


class TemplatesConfig(BaseModel):
    """On-device template engine configuration."""
    enabled: bool = True
    # Directory with user-defined template YAML files
    user_templates_dir: str = "~/.remark-bridge/templates"
    # Folder on the tablet where pushed templates appear
    target_folder: str = "Templates"


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = "~/.remark-bridge/bridge.log"
    max_size_mb: int = 50
    backup_count: int = 5


class AppConfig(BaseModel):
    remarkable: RemarkableConfig = Field(default_factory=RemarkableConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    response: ResponseConfig = Field(default_factory=ResponseConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    microsoft: MicrosoftConfig = Field(default_factory=MicrosoftConfig)
    reverse_sync: ReverseSyncConfig = Field(default_factory=ReverseSyncConfig)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    templates: TemplatesConfig = Field(default_factory=TemplatesConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load config from YAML file, falling back to defaults for missing keys."""
    path = Path(path).expanduser()
    if not path.exists():
        return AppConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    return AppConfig(**raw)


def resolve_path(path_str: str) -> Path:
    """Expand ~ and env vars, return absolute Path."""
    return Path(path_str).expanduser().resolve()
