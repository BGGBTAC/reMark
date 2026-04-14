"""Tests for the plugin system and related state tables."""


import pytest

from src.config import PluginConfig
from src.plugins.hooks import (
    ActionExtractorHook,
    NoteProcessorHook,
    Plugin,
    PluginMetadata,
    SyncHook,
)
from src.plugins.registry import PluginRegistry
from src.sync.state import SyncState

# =====================
# Hook base classes
# =====================

class TestHooks:
    def test_plugin_metadata_defaults(self):
        m = PluginMetadata(name="test")
        assert m.name == "test"
        assert m.version == "0.0.1"
        assert m.description == ""

    def test_concrete_plugin_must_define_metadata(self):
        with pytest.raises(TypeError):
            Plugin()  # abstract


class DummyExtractor(ActionExtractorHook):
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="dummy-extractor", version="1.0")

    async def extract(self, text: str, context: dict) -> list[dict]:
        return [{"task": f"processed: {text[:10]}"}]


class DummyProcessor(NoteProcessorHook):
    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="dummy-processor")

    async def process(self, content: str, frontmatter: dict) -> tuple[str, dict]:
        frontmatter["touched_by"] = "dummy"
        return content + "\n\n<!-- processed -->", frontmatter


class DummySyncHook(SyncHook):
    def __init__(self):
        self.before_called = 0
        self.after_called = 0

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(name="dummy-sync")

    async def before_sync(self, context: dict) -> None:
        self.before_called += 1

    async def after_sync(self, context: dict, report: dict) -> None:
        self.after_called += 1


# =====================
# Registry — direct registration
# =====================

class TestRegistryDirect:
    def test_disabled_config_loads_nothing(self, tmp_path):
        reg = PluginRegistry(PluginConfig(enabled=False, plugin_dir=str(tmp_path)))
        reg.discover()
        assert reg.list_plugins() == []

    def test_no_plugins_dir(self, tmp_path):
        reg = PluginRegistry(PluginConfig(plugin_dir=str(tmp_path / "missing")))
        reg.discover()
        assert reg.list_plugins() == []

    def test_loads_from_directory(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()

        # Write a plugin file
        (plugin_dir / "my_plugin.py").write_text('''
from src.plugins.hooks import ActionExtractorHook, PluginMetadata

class MyPlugin(ActionExtractorHook):
    @property
    def metadata(self):
        return PluginMetadata(name="my-plugin", version="0.2")

    async def extract(self, text, context):
        return []
''')

        reg = PluginRegistry(PluginConfig(plugin_dir=str(plugin_dir)))
        reg.discover()

        entries = reg.list_plugins()
        assert len(entries) == 1
        assert entries[0]["name"] == "my-plugin"
        assert entries[0]["version"] == "0.2"
        assert "ActionExtractorHook" in entries[0]["hooks"]

    def test_skips_underscore_files(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "_private.py").write_text("# ignored")
        (plugin_dir / "__init__.py").write_text("# also ignored")

        reg = PluginRegistry(PluginConfig(plugin_dir=str(plugin_dir)))
        reg.discover()
        assert reg.list_plugins() == []

    def test_disabled_plugins_skipped(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "p.py").write_text('''
from src.plugins.hooks import ActionExtractorHook, PluginMetadata

class P(ActionExtractorHook):
    @property
    def metadata(self):
        return PluginMetadata(name="skip-me")

    async def extract(self, text, context):
        return []
''')

        reg = PluginRegistry(PluginConfig(
            plugin_dir=str(plugin_dir),
            disabled=["skip-me"],
        ))
        reg.discover()
        assert reg.list_plugins() == []

    def test_broken_plugin_doesnt_crash_registry(self, tmp_path):
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "bad.py").write_text("this is not valid python !")
        (plugin_dir / "good.py").write_text('''
from src.plugins.hooks import ActionExtractorHook, PluginMetadata

class G(ActionExtractorHook):
    @property
    def metadata(self):
        return PluginMetadata(name="good-plugin")

    async def extract(self, text, context):
        return []
''')

        reg = PluginRegistry(PluginConfig(plugin_dir=str(plugin_dir)))
        reg.discover()
        names = [p["name"] for p in reg.list_plugins()]
        assert "good-plugin" in names

    def test_hook_filtering(self, tmp_path):
        reg = PluginRegistry(PluginConfig(plugin_dir=str(tmp_path)))
        # Manually instantiate to test filtering
        extractor = DummyExtractor()
        processor = DummyProcessor()
        reg._plugins["a"] = extractor
        reg._plugins["b"] = processor
        reg._by_hook.setdefault(ActionExtractorHook, []).append(extractor)
        reg._by_hook.setdefault(NoteProcessorHook, []).append(processor)

        assert reg.hooks(ActionExtractorHook) == [extractor]
        assert reg.hooks(NoteProcessorHook) == [processor]
        assert reg.hooks(SyncHook) == []

    def test_get_returns_instance(self, tmp_path):
        reg = PluginRegistry(PluginConfig(plugin_dir=str(tmp_path)))
        extractor = DummyExtractor()
        reg._plugins["dummy-extractor"] = extractor
        assert reg.get("dummy-extractor") is extractor
        assert reg.get("nonexistent") is None


# =====================
# Example plugin
# =====================

class TestExamplePlugin:
    @pytest.mark.asyncio
    async def test_at_mention_extraction(self):
        from src.plugins.examples.at_mention_extractor import AtMentionExtractor

        plugin = AtMentionExtractor()
        assert plugin.metadata.name == "at-mention-extractor"

        text = "Meeting notes.\n@alice should review.\nAlso @bob owns the API."
        actions = await plugin.extract(text, context={})

        assert len(actions) == 2
        assignees = {a["assignee"] for a in actions}
        assert assignees == {"alice", "bob"}
        assert all(a["priority"] == "high" for a in actions)

    @pytest.mark.asyncio
    async def test_deduplicates_mentions(self):
        from src.plugins.examples.at_mention_extractor import AtMentionExtractor

        plugin = AtMentionExtractor()
        text = "@alice check this\n@alice also this"
        actions = await plugin.extract(text, context={})
        assert len(actions) == 1


# =====================
# State — plugin_state
# =====================

class TestPluginState:
    def test_register_and_list(self, tmp_path):
        state = SyncState(tmp_path / "p.db")
        state.register_plugin("plugin-a")
        state.register_plugin("plugin-b", config="some config")

        plugins = state.list_plugins()
        names = {p["name"] for p in plugins}
        assert names == {"plugin-a", "plugin-b"}
        state.close()

    def test_enable_disable(self, tmp_path):
        state = SyncState(tmp_path / "pd.db")
        state.register_plugin("p")
        assert state.is_plugin_enabled("p")

        state.set_plugin_enabled("p", False)
        assert not state.is_plugin_enabled("p")

        state.set_plugin_enabled("p", True)
        assert state.is_plugin_enabled("p")
        state.close()

    def test_unregistered_defaults_enabled(self, tmp_path):
        state = SyncState(tmp_path / "u.db")
        # Not registered yet → default to enabled
        assert state.is_plugin_enabled("never-seen")
        state.close()


# =====================
# State — reverse_push_queue
# =====================

class TestReversePushQueue:
    def test_enqueue_and_list(self, tmp_path):
        state = SyncState(tmp_path / "r.db")
        assert state.enqueue_reverse_push("/v/note.md") is True
        assert state.enqueue_reverse_push("/v/note.md") is False  # duplicate

        queue = state.get_reverse_queue()
        assert len(queue) == 1
        assert queue[0]["vault_path"] == "/v/note.md"
        state.close()

    def test_mark_pushed(self, tmp_path):
        state = SyncState(tmp_path / "rp.db")
        state.enqueue_reverse_push("/v/a.md")
        state.mark_reverse_pushed("/v/a.md", "rm-doc-123")

        pending = state.get_reverse_queue(status="pending")
        assert pending == []

        pushed = state.get_reverse_queue(status="pushed")
        assert len(pushed) == 1
        assert pushed[0]["remarkable_doc_id"] == "rm-doc-123"
        state.close()

    def test_mark_failed(self, tmp_path):
        state = SyncState(tmp_path / "rf.db")
        state.enqueue_reverse_push("/v/bad.md")
        state.mark_reverse_failed("/v/bad.md", "upload timeout")

        errored = state.get_reverse_queue(status="error")
        assert len(errored) == 1
        assert errored[0]["error"] == "upload timeout"
        state.close()


# =====================
# State — webpush_subscriptions
# =====================

class TestWebPushSubscriptions:
    def test_add_and_list(self, tmp_path):
        state = SyncState(tmp_path / "wp.db")
        row_id = state.add_webpush_subscription(
            endpoint="https://push.example/abc",
            p256dh="pub-key",
            auth="auth-token",
            user_agent="Firefox",
        )
        assert row_id > 0

        subs = state.list_webpush_subscriptions()
        assert len(subs) == 1
        assert subs[0]["endpoint"] == "https://push.example/abc"
        state.close()

    def test_replace_on_duplicate(self, tmp_path):
        state = SyncState(tmp_path / "wp2.db")
        state.add_webpush_subscription("endpoint-1", "k1", "a1")
        state.add_webpush_subscription("endpoint-1", "k2", "a2")

        subs = state.list_webpush_subscriptions()
        assert len(subs) == 1
        assert subs[0]["p256dh"] == "k2"
        state.close()

    def test_remove(self, tmp_path):
        state = SyncState(tmp_path / "wp3.db")
        state.add_webpush_subscription("endpoint-1", "k", "a")
        state.remove_webpush_subscription("endpoint-1")
        assert state.list_webpush_subscriptions() == []
        state.close()


# =====================
# State — template_instances
# =====================

class TestTemplateInstances:
    def test_record_and_lookup(self, tmp_path):
        state = SyncState(tmp_path / "t.db")
        state.record_template_push("doc-xyz", "meeting")

        entry = state.get_template_for_doc("doc-xyz")
        assert entry is not None
        assert entry["template_name"] == "meeting"
        assert entry["filled_at"] is None
        state.close()

    def test_mark_filled(self, tmp_path):
        state = SyncState(tmp_path / "tf.db")
        state.record_template_push("doc-1", "daily")
        state.mark_template_filled("doc-1", "/vault/Notes/daily.md")

        entry = state.get_template_for_doc("doc-1")
        assert entry["filled_at"] is not None
        assert entry["vault_path"] == "/vault/Notes/daily.md"
        state.close()

    def test_missing_returns_none(self, tmp_path):
        state = SyncState(tmp_path / "tn.db")
        assert state.get_template_for_doc("nope") is None
        state.close()
