"""Tests for the editable /settings web UI."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from src.config import ProcessingConfig, SearchConfig
from src.web.config_writer import MASK, load_yaml, update_section, write_yaml
from src.web.settings_forms import build_form, parse_form


class TestConfigWriterRoundtrip:
    def test_preserves_comments(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text(
            "# top comment\n"
            "processing:\n"
            "  # keep me\n"
            "  model: claude-sonnet-4\n"
            "  extract_actions: true\n",
            encoding="utf-8",
        )
        data = load_yaml(p)
        data["processing"]["extract_actions"] = False
        write_yaml(p, data)
        text = p.read_text(encoding="utf-8")
        assert "# top comment" in text
        assert "# keep me" in text
        assert "extract_actions: false" in text

    def test_atomic_write_creates_parent(self, tmp_path):
        p = tmp_path / "nested" / "c.yaml"
        write_yaml(p, {"a": 1})
        assert p.exists()
        assert load_yaml(p) == {"a": 1}

    def test_update_section_merges(self, tmp_path):
        p = tmp_path / "c.yaml"
        write_yaml(p, {"processing": {"model": "old", "extract_actions": True}})
        update_section(p, "processing", {"model": "new"})
        assert load_yaml(p)["processing"]["model"] == "new"
        # Untouched key preserved
        assert load_yaml(p)["processing"]["extract_actions"] is True

    def test_update_section_honours_mask(self, tmp_path):
        p = tmp_path / "c.yaml"
        write_yaml(p, {"microsoft": {"client_id": "real-secret"}})
        update_section(
            p, "microsoft",
            {"client_id": MASK},
            secret_keys={"client_id"},
        )
        assert load_yaml(p)["microsoft"]["client_id"] == "real-secret"

    def test_update_section_refuses_non_mapping(self, tmp_path):
        p = tmp_path / "c.yaml"
        write_yaml(p, {"processing": "not a dict"})
        with pytest.raises(ValueError):
            update_section(p, "processing", {"model": "x"})

    def test_dotted_keys_nest(self, tmp_path):
        p = tmp_path / "c.yaml"
        write_yaml(p, {"obsidian": {}})
        update_section(p, "obsidian", {"git.auto_push": False})
        assert load_yaml(p)["obsidian"]["git"]["auto_push"] is False


class TestFormBuilder:
    def test_basic_fields(self):
        form = build_form(ProcessingConfig, ProcessingConfig())
        names = [f.name for f in form.fields]
        assert "model" in names
        assert "extract_actions" in names

    def test_bool_field_has_bool_kind(self):
        form = build_form(ProcessingConfig, ProcessingConfig())
        by_name = {f.name: f for f in form.fields}
        assert by_name["extract_actions"].kind == "bool"
        assert by_name["extract_actions"].value is True

    def test_literal_becomes_select_with_choices(self):
        form = build_form(SearchConfig, SearchConfig())
        by_name = {f.name: f for f in form.fields}
        backend = by_name["backend"]
        assert backend.kind == "select"
        assert set(backend.choices) == {"voyage", "openai", "local"}

    def test_secret_fields_mask_when_populated(self):
        class _Secret(BaseModel):
            client_id: str = "real-value"
            label: str = "ok"

        form = build_form(_Secret, _Secret())
        by_name = {f.name: f for f in form.fields}
        assert by_name["client_id"].kind == "password"
        assert by_name["client_id"].value == MASK
        # Non-secret field is untouched
        assert by_name["label"].value == "ok"

    def test_nested_model_becomes_subgroup(self):
        from src.config import ObsidianConfig

        form = build_form(ObsidianConfig, ObsidianConfig())
        # `git` is a nested GitConfig → subgroup
        titles = [sg.title for sg in form.subgroups]
        assert any("Git" in t or "git" in t for t in titles)


class TestFormParser:
    def test_bool_checkbox_missing_means_false(self):
        parsed = parse_form(ProcessingConfig, {"model": "claude"})
        assert parsed["extract_actions"] is False

    def test_bool_checkbox_present_means_true(self):
        parsed = parse_form(
            ProcessingConfig,
            {"extract_actions": "true", "model": "claude"},
        )
        assert parsed["extract_actions"] is True

    def test_list_textarea_splits_lines(self):
        from src.config import RemarkableConfig

        parsed = parse_form(
            RemarkableConfig,
            {
                "device_token_path": "~/x",
                "sync_folders": "Work\nPersonal\n",
                "ignore_folders": "Trash",
                "response_folder": "R",
            },
        )
        assert parsed["sync_folders"] == ["Work", "Personal"]
        assert parsed["ignore_folders"] == ["Trash"]

    def test_mask_sentinel_propagates(self):
        class _M(BaseModel):
            client_id: str = ""

        parsed = parse_form(_M, {"client_id": MASK})
        assert parsed["client_id"] == MASK

    def test_nested_model_parses(self):
        from src.config import ObsidianConfig

        parsed = parse_form(
            ObsidianConfig,
            {
                "vault_path": "/v",
                "folder_map": '{"_default": "Inbox"}',
                "git.enabled": "true",
                "git.remote": "origin",
                "git.branch": "main",
                "git.auto_commit": "true",
                "git.auto_push": "true",
                "git.commit_message_template": "sync: {count}",
            },
        )
        assert parsed["git"]["enabled"] is True
        assert parsed["git"]["remote"] == "origin"


class TestEditableRoutes:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        import yaml

        from src.config import AppConfig
        from src.web.app import create_app

        # Minimal usable config on disk so update_section has something
        # to merge into.
        cfg_path = tmp_path / "config.yaml"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        cfg_yaml = {
            "processing": {"model": "old-model", "extract_actions": True},
            "sync": {"state_db": str(state_dir / "state.db")},
            "obsidian": {"vault_path": str(tmp_path / "vault")},
        }
        (tmp_path / "vault").mkdir()
        cfg_path.write_text(yaml.safe_dump(cfg_yaml), encoding="utf-8")
        monkeypatch.setenv("REMARK_CONFIG", str(cfg_path))

        config = AppConfig(**cfg_yaml)
        # Point the state DB at the temp path so the audit log write
        # doesn't bleed into the developer's real home dir.
        config.sync.state_db = str(state_dir / "state.db")
        config.obsidian.vault_path = str(tmp_path / "vault")
        return create_app(config), cfg_path

    def test_index_lists_sections(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app[0])
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Processing" in resp.text or "processing" in resp.text

    def test_section_get_renders_form(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app[0])
        resp = client.get("/settings/processing")
        assert resp.status_code == 200
        assert 'name="model"' in resp.text
        assert "old-model" in resp.text

    def test_unknown_section_returns_404(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app[0])
        resp = client.get("/settings/does-not-exist")
        assert resp.status_code == 404

    def test_section_post_writes_yaml(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app[0], follow_redirects=False)
        resp = client.post(
            "/settings/processing",
            data={
                "model": "new-model-from-ui",
                "api_key_env": "ANTHROPIC_API_KEY",
                "extract_actions": "true",
                "extract_tags": "true",
                "generate_summary": "true",
                "actions.action_colors": "6",
                "actions.question_colors": "5",
                "actions.highlight_colors": "3",
                "actions.detect_from_text": "true",
            },
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/settings/processing?saved=")

        cfg_path: Path = app[1]
        body = cfg_path.read_text(encoding="utf-8")
        assert "new-model-from-ui" in body
