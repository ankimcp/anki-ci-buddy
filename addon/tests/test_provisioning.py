"""Unit tests for the provisioning engine, driven with a fake ProfileManager.

Covers spec §8 items 6-10:
  6. newer serial → inject; same/older → no-op
  7. endpoint outside allowlist → ignored, default kept
  8. corrupt/partial/absent file → no crash, retried next tick
  9. clear_key_on_close → key cleared on profile close
 10. hkey never appears in any log output (fingerprint only)
"""

import json
import sys
import types

import pytest

from ci_buddy import core
from ci_buddy.provisioning import (
    AddonConfigProvisioner,
    CollectionConfigProvisioner,
    SyncProvisioner,
    register,
)


class FakePM:
    """Minimal stand-in for aqt's ProfileManager, recording every call."""

    def __init__(self):
        self.profile = {}  # truthy → "a profile is open"
        self.sync_key = "UNSET"
        self.sync_username = "UNSET"
        self.custom_sync_url = "UNSET"
        self.saves = 0
        self.calls: list[tuple] = []

    def set_sync_key(self, val):
        self.sync_key = val
        self.calls.append(("set_sync_key", val))

    def set_sync_username(self, val):
        self.sync_username = val
        self.calls.append(("set_sync_username", val))

    def set_custom_sync_url(self, url):
        self.custom_sync_url = url
        self.calls.append(("set_custom_sync_url", url))

    def clear_sync_auth(self):
        # Mirrors aqt/profiles.py clear_sync_auth: clears key + username (+ host
        # number + currentSyncUrl, which this fake does not model separately).
        self.sync_key = None
        self.sync_username = None
        self.calls.append(("clear_sync_auth",))

    def save(self):
        self.saves += 1
        self.calls.append(("save",))


def install_fake_aqt(monkeypatch, pm):
    """Install a fake ``aqt`` / ``aqt.qt`` into sys.modules so the lazy imports
    inside SyncProvisioner resolve without a running Anki (spec §8)."""
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(pm=pm)

    qt_mod = types.ModuleType("aqt.qt")

    class FakeQTimer:
        def __init__(self, *a):
            pass

        def setInterval(self, *a):
            pass

        @property
        def timeout(self):
            return types.SimpleNamespace(connect=lambda *a, **k: None)

        def start(self):
            pass

    qt_mod.QTimer = FakeQTimer
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    monkeypatch.setitem(sys.modules, "aqt.qt", qt_mod)
    return aqt_mod.mw


@pytest.fixture
def logs():
    return []


def make_provisioner(tmp_path, logs, **overrides):
    config = core.merge_config(
        {
            "provisioning_enabled": True,
            "credentials_path": str(tmp_path / "sync-credentials.json"),
            **overrides,
        }
    )
    return SyncProvisioner(config, printer=logs.append), config


def write_creds(path, **fields):
    payload = {"v": 1, **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- item 6: serial gating ---------------------------------------------- #


def test_newer_serial_injects(tmp_path, logs):
    prov, config = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=1, hkey="key-one")
    pm = FakePM()

    assert prov.maybe_inject(pm, force=True) is True
    assert pm.sync_key == "key-one"
    assert pm.saves == 1
    assert prov.last_applied_serial == 1


def test_same_serial_is_noop(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=5, hkey="key-a")
    pm = FakePM()

    assert prov.maybe_inject(pm, force=True) is True
    # rewrite with the SAME serial but a new key — must be ignored
    write_creds(path, serial=5, hkey="key-b")
    assert prov.maybe_inject(pm, force=True) is False
    assert pm.sync_key == "key-a"  # unchanged


def test_older_serial_is_noop(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=9, hkey="key-new")
    pm = FakePM()
    prov.maybe_inject(pm, force=True)

    write_creds(path, serial=3, hkey="key-rollback")  # rollback
    assert prov.maybe_inject(pm, force=True) is False
    assert pm.sync_key == "key-new"


def test_increasing_serial_reinjects(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    pm = FakePM()

    write_creds(path, serial=1, hkey="k1")
    assert prov.maybe_inject(pm, force=True) is True
    write_creds(path, serial=2, hkey="k2")
    assert prov.maybe_inject(pm, force=True) is True
    assert pm.sync_key == "k2"
    assert prov.last_applied_serial == 2


# --- item 7: endpoint allowlist ----------------------------------------- #


def test_allowlisted_endpoint_applied(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(
        path, serial=1, hkey="k", endpoint="https://sync.ankiweb.net/"
    )
    pm = FakePM()
    prov.maybe_inject(pm, force=True)
    assert pm.custom_sync_url == "https://sync.ankiweb.net/"


def test_rogue_endpoint_ignored_default_kept(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=1, hkey="k", endpoint="https://evil.example.com/")
    pm = FakePM()
    prov.maybe_inject(pm, force=True)
    # custom URL setter never called → default kept
    assert pm.custom_sync_url == "UNSET"
    assert ("set_custom_sync_url", "https://evil.example.com/") not in pm.calls
    assert pm.sync_key == "k"  # key still applied
    assert any("not in allowlist" in line for line in logs)


def test_no_endpoint_reverts_to_default(tmp_path, logs):
    # HIGH-1: an omitted endpoint must revert to default AnkiWeb by clearing any
    # custom URL — set_custom_sync_url(None) — not merely skip the setter.
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=1, hkey="k")  # no endpoint
    pm = FakePM()
    prov.maybe_inject(pm, force=True)
    assert pm.custom_sync_url is None
    assert ("set_custom_sync_url", None) in pm.calls


def test_endpoint_then_no_endpoint_clears_stale_custom_url(tmp_path, logs):
    # HIGH-1: serial N sets a custom endpoint; serial N+1 omits it → the stale
    # custom URL must be cleared so prefs21.db carries no endpoint residue.
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    pm = FakePM()

    write_creds(path, serial=1, hkey="k1", endpoint="https://sync.ankiweb.net/")
    assert prov.maybe_inject(pm, force=True) is True
    assert pm.custom_sync_url == "https://sync.ankiweb.net/"

    write_creds(path, serial=2, hkey="k2")  # endpoint dropped
    assert prov.maybe_inject(pm, force=True) is True
    assert pm.custom_sync_url is None  # stale custom URL cleared


def test_username_applied_when_present(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=1, hkey="k", username="alice@example.com")
    pm = FakePM()
    prov.maybe_inject(pm, force=True)
    assert pm.sync_username == "alice@example.com"


# --- item 8: bad / absent files are safe -------------------------------- #


def test_absent_file_is_safe_and_warns_once(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)  # file never written
    pm = FakePM()
    assert prov.maybe_inject(pm, force=True) is False
    assert prov.maybe_inject(pm, force=True) is False
    assert pm.calls == []
    # exactly one "not found" warning despite two attempts
    not_found = [l for l in logs if "not found" in l]
    assert len(not_found) == 1


@pytest.mark.parametrize("content", ["", "{", "not json", "[]", '{"serial":1}'])
def test_corrupt_partial_file_is_safe(tmp_path, logs, content):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    path.write_text(content, encoding="utf-8")
    pm = FakePM()
    assert prov.maybe_inject(pm, force=True) is False
    assert pm.calls == []


def test_recovers_after_corrupt_then_valid(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    pm = FakePM()

    path.write_text("garbage", encoding="utf-8")
    assert prov.maybe_inject(pm, force=True) is False

    write_creds(path, serial=1, hkey="good-key")
    assert prov.maybe_inject(pm, force=True) is True
    assert pm.sync_key == "good-key"


def test_profile_did_open_noop_when_pm_none(tmp_path, logs, monkeypatch):
    # MEDIUM-3: mw.pm is None (very early / between profiles) → no injection,
    # no crash. The timer path (_start_timer) must also survive.
    prov, _ = make_provisioner(tmp_path, logs)
    write_creds(tmp_path / "sync-credentials.json", serial=1, hkey="k")
    install_fake_aqt(monkeypatch, pm=None)

    prov.on_profile_did_open()  # must not raise
    assert prov.last_applied_serial is None


def test_profile_did_open_noop_when_profile_none(tmp_path, logs, monkeypatch):
    # MEDIUM-3: mw.pm exists but pm.profile is None (open/close transition) →
    # no injection, no crash.
    prov, _ = make_provisioner(tmp_path, logs)
    write_creds(tmp_path / "sync-credentials.json", serial=1, hkey="k")
    pm = FakePM()
    pm.profile = None
    install_fake_aqt(monkeypatch, pm=pm)

    prov.on_profile_did_open()  # must not raise
    assert pm.calls == []  # nothing injected
    assert prov.last_applied_serial is None


def test_poll_noop_when_pm_none(tmp_path, logs, monkeypatch):
    # MEDIUM-3: the poll path is equally guarded.
    prov, _ = make_provisioner(tmp_path, logs)
    write_creds(tmp_path / "sync-credentials.json", serial=1, hkey="k")
    install_fake_aqt(monkeypatch, pm=None)

    prov._poll()  # must not raise
    assert prov.last_applied_serial is None


def test_poll_noop_when_profile_none(tmp_path, logs, monkeypatch):
    prov, _ = make_provisioner(tmp_path, logs)
    write_creds(tmp_path / "sync-credentials.json", serial=1, hkey="k")
    pm = FakePM()
    pm.profile = None
    install_fake_aqt(monkeypatch, pm=pm)

    prov._poll()  # must not raise
    assert pm.calls == []
    assert prov.last_applied_serial is None


# --- item 9: clear on close --------------------------------------------- #


def test_clear_key_clears_and_saves(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    pm = FakePM()
    pm.sync_key = "live-key"
    prov.last_applied_serial = 5

    prov.clear_key(pm)
    assert pm.sync_key is None
    assert pm.sync_username is None
    assert pm.saves == 1
    # forces a fresh re-inject next open
    assert prov.last_applied_serial is None


def test_clear_key_clears_endpoint_state(tmp_path, logs):
    # HIGH-1: teardown must clear endpoint residue too (clear_sync_auth handles
    # currentSyncUrl; we additionally reset customSyncUrl).
    prov, _ = make_provisioner(tmp_path, logs)
    pm = FakePM()
    pm.custom_sync_url = "https://sync.ankiweb.net/"

    prov.clear_key(pm)
    assert ("clear_sync_auth",) in pm.calls
    assert pm.custom_sync_url is None


def test_clear_then_reinject_on_reopen(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=4, hkey="k4")
    pm = FakePM()

    prov.maybe_inject(pm, force=True)
    assert prov.last_applied_serial == 4
    prov.clear_key(pm)
    # same serial file still on disk → after clear it must re-inject (fresh state)
    assert prov.maybe_inject(pm, force=True) is True
    assert pm.sync_key == "k4"


# --- item 10: hkey never logged ----------------------------------------- #


def test_hkey_never_appears_in_logs(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    secret = "TOP-SECRET-HKEY-abcdef123456"
    write_creds(path, serial=1, hkey=secret, endpoint="https://evil.example.com/")
    pm = FakePM()
    prov.maybe_inject(pm, force=True)
    prov.clear_key(pm)

    joined = "\n".join(logs)
    assert secret not in joined
    # but a fingerprint should be present
    assert core.hkey_fingerprint(secret) in joined


# --- mtime fast-path ---------------------------------------------------- #


def test_mtime_fastpath_skips_unchanged_file(tmp_path, logs):
    prov, _ = make_provisioner(tmp_path, logs)
    path = tmp_path / "sync-credentials.json"
    write_creds(path, serial=1, hkey="k")
    pm = FakePM()

    # first non-forced poll reads + injects
    assert prov.maybe_inject(pm, force=False) is True
    calls_after_first = len(pm.calls)
    # second non-forced poll: unchanged mtime → skipped entirely
    assert prov.maybe_inject(pm, force=False) is False
    assert len(pm.calls) == calls_after_first


# --- RENDER_LATEX collection provisioning ------------------------------- #


def install_fake_anki_config(monkeypatch):
    """Install a fake ``anki`` / ``anki.config`` so the lazy ``from anki.config
    import Config`` inside CollectionConfigProvisioner resolves without Anki."""
    anki_mod = types.ModuleType("anki")
    config_mod = types.ModuleType("anki.config")

    class Config:
        class Bool:
            RENDER_LATEX = "RENDER_LATEX"

    config_mod.Config = Config
    anki_mod.config = config_mod
    monkeypatch.setitem(sys.modules, "anki", anki_mod)
    monkeypatch.setitem(sys.modules, "anki.config", config_mod)
    return Config


class FakeCollection:
    """Minimal stand-in for anki's Collection config accessors."""

    def __init__(self, latex_enabled=False):
        self._latex = latex_enabled
        self.calls: list[tuple] = []

    def get_config_bool(self, key):
        self.calls.append(("get", key))
        return self._latex

    def set_config_bool(self, key, val):
        self.calls.append(("set", key, val))
        self._latex = val


class FakeHook:
    def __init__(self):
        self.callbacks: list = []

    def append(self, cb):
        self.callbacks.append(cb)


def install_fake_gui_hooks(monkeypatch, pm=None, col=None):
    """Install a fake ``aqt`` exposing ``gui_hooks`` with recording hook lists
    and an ``mw`` carrying ``pm`` / ``col``."""
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(pm=pm, col=col)
    gui_hooks = types.SimpleNamespace(
        profile_did_open=FakeHook(),
        profile_will_close=FakeHook(),
        collection_did_load=FakeHook(),
        sync_did_finish=FakeHook(),
    )
    aqt_mod.gui_hooks = gui_hooks
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    return gui_hooks


def test_ensure_render_latex_sets_when_off(monkeypatch, logs):
    install_fake_anki_config(monkeypatch)
    prov = CollectionConfigProvisioner(core.merge_config({}), printer=logs.append)
    col = FakeCollection(latex_enabled=False)

    assert prov.ensure_render_latex(col) is True
    assert col._latex is True
    assert any(c[0] == "set" and c[2] is True for c in col.calls)
    # observability: the enable path emits exactly one greppable log line
    latex_logs = [l for l in logs if "force-enabled RENDER_LATEX" in l]
    assert len(latex_logs) == 1


def test_ensure_render_latex_idempotent_when_already_on(monkeypatch, logs):
    install_fake_anki_config(monkeypatch)
    prov = CollectionConfigProvisioner(core.merge_config({}), printer=logs.append)
    col = FakeCollection(latex_enabled=True)

    # already on → no write (must not re-dirty the collection)
    assert prov.ensure_render_latex(col) is False
    assert not any(c[0] == "set" for c in col.calls)
    # the anti-loop no-op stays silent — no force-enable log line
    assert not any("force-enabled RENDER_LATEX" in l for l in logs)


def test_ensure_render_latex_noop_when_no_collection(monkeypatch, logs):
    install_fake_anki_config(monkeypatch)
    prov = CollectionConfigProvisioner(core.merge_config({}), printer=logs.append)
    assert prov.ensure_render_latex(None) is False


def test_on_collection_did_load_enables_latex(monkeypatch, logs):
    install_fake_anki_config(monkeypatch)
    prov = CollectionConfigProvisioner(core.merge_config({}), printer=logs.append)
    col = FakeCollection(latex_enabled=False)

    prov.on_collection_did_load(col)
    assert col._latex is True


def test_on_sync_did_finish_enables_latex(monkeypatch, logs):
    install_fake_anki_config(monkeypatch)
    col = FakeCollection(latex_enabled=False)
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(col=col)
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)

    prov = CollectionConfigProvisioner(core.merge_config({}), printer=logs.append)
    prov.on_sync_did_finish()
    assert col._latex is True


def test_register_wires_latex_hooks_when_flag_on(monkeypatch):
    gui_hooks = install_fake_gui_hooks(monkeypatch)
    register(
        core.merge_config(
            {"ensure_latex_generation": True, "provisioning_enabled": False}
        )
    )
    assert len(gui_hooks.collection_did_load.callbacks) == 1
    assert len(gui_hooks.sync_did_finish.callbacks) == 1
    # provisioning disabled → no credential hooks wired
    assert gui_hooks.profile_did_open.callbacks == []
    assert gui_hooks.profile_will_close.callbacks == []


def test_register_skips_latex_hooks_when_flag_off(monkeypatch):
    gui_hooks = install_fake_gui_hooks(monkeypatch)
    register(
        core.merge_config(
            {"ensure_latex_generation": False, "provisioning_enabled": False}
        )
    )
    assert gui_hooks.collection_did_load.callbacks == []
    assert gui_hooks.sync_did_finish.callbacks == []


# --- hide AnkiMCP toolbar indicator ------------------------------------- #


class FakeAddonManager:
    """Minimal stand-in for aqt's AddonManager (getConfig/writeConfig)."""

    def __init__(self, configs=None):
        # module -> merged config dict (what getConfig returns)
        self._configs = dict(configs or {})
        self.writes: list[tuple] = []

    def getConfig(self, module):
        return self._configs.get(module)

    def writeConfig(self, module, conf):
        self.writes.append((module, conf))
        self._configs[module] = conf


def make_addon_provisioner(logs, **overrides):
    config = core.merge_config(overrides)
    return AddonConfigProvisioner(config, printer=logs.append)


def test_hide_indicator_flips_true_to_false(logs):
    prov = make_addon_provisioner(logs)
    mgr = FakeAddonManager({core.ANKIMCP_PACKAGE: {"show_toolbar_indicator": True}})

    assert prov.hide_ankimcp_indicator(mgr) is True
    assert mgr.writes == [(core.ANKIMCP_PACKAGE, {"show_toolbar_indicator": False})]
    assert any("hid AnkiMCP toolbar indicator" in l for l in logs)


def test_hide_indicator_idempotent_when_already_false(logs):
    prov = make_addon_provisioner(logs)
    mgr = FakeAddonManager({core.ANKIMCP_PACKAGE: {"show_toolbar_indicator": False}})

    assert prov.hide_ankimcp_indicator(mgr) is False
    assert mgr.writes == []  # never re-writes an already-hidden indicator


def test_hide_indicator_noop_when_ankimcp_absent(logs):
    prov = make_addon_provisioner(logs)
    mgr = FakeAddonManager({})  # AnkiMCP not installed → getConfig returns None

    assert prov.hide_ankimcp_indicator(mgr) is False
    assert mgr.writes == []


def test_hide_indicator_noop_when_no_manager(logs):
    prov = make_addon_provisioner(logs)
    assert prov.hide_ankimcp_indicator(None) is False


def test_hide_indicator_uses_original_writeconfig_through_shim(logs):
    # The core interaction: locks Seam 4 replaces writeConfig with a shim that
    # DROPS writes but stashes the original on itself under
    # core.ORIGINAL_WRITE_CONFIG_ATTR (the shared contract constant; the
    # producer side is asserted in test_locks.py). The provisioner must recover
    # and use that original so the write persists.
    real_writes: list[tuple] = []

    def real_write_config(module, conf):
        real_writes.append((module, conf))

    def dropping_shim(module, conf):
        # mimics locks._blocked_write_config — silently drops the write
        pass

    dropping_shim._ci_buddy_shim = True
    setattr(dropping_shim, core.ORIGINAL_WRITE_CONFIG_ATTR, real_write_config)

    mgr = types.SimpleNamespace(
        getConfig=lambda module: {"show_toolbar_indicator": True}
        if module == core.ANKIMCP_PACKAGE
        else None,
        writeConfig=dropping_shim,
    )

    prov = make_addon_provisioner(logs)
    assert prov.hide_ankimcp_indicator(mgr) is True
    # persisted via the ORIGINAL, not dropped by the shim
    assert real_writes == [(core.ANKIMCP_PACKAGE, {"show_toolbar_indicator": False})]


def test_real_write_config_falls_through_without_shim():
    mgr = FakeAddonManager()
    # no shim installed → the live writeConfig is already the real one
    from ci_buddy.provisioning import _real_write_config

    # (== not is: mgr.writeConfig yields a fresh bound-method wrapper each access)
    assert _real_write_config(mgr) == mgr.writeConfig


def test_register_hides_indicator_at_load_time(monkeypatch):
    # register() must coerce the indicator immediately (not on a hook), using
    # the addonManager on mw.
    mgr = FakeAddonManager({core.ANKIMCP_PACKAGE: {"show_toolbar_indicator": True}})
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(addonManager=mgr)
    aqt_mod.gui_hooks = types.SimpleNamespace(
        profile_did_open=FakeHook(),
        profile_will_close=FakeHook(),
        collection_did_load=FakeHook(),
        sync_did_finish=FakeHook(),
    )
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)

    register(
        core.merge_config(
            {
                "provisioning_enabled": False,
                "ensure_latex_generation": False,
                "hide_ankimcp_toolbar_indicator": True,
            }
        )
    )
    assert mgr.writes == [(core.ANKIMCP_PACKAGE, {"show_toolbar_indicator": False})]


def test_register_skips_indicator_when_flag_off(monkeypatch):
    mgr = FakeAddonManager({core.ANKIMCP_PACKAGE: {"show_toolbar_indicator": True}})
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = types.SimpleNamespace(addonManager=mgr)
    aqt_mod.gui_hooks = types.SimpleNamespace(
        profile_did_open=FakeHook(),
        profile_will_close=FakeHook(),
        collection_did_load=FakeHook(),
        sync_did_finish=FakeHook(),
    )
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)

    register(
        core.merge_config(
            {
                "provisioning_enabled": False,
                "ensure_latex_generation": False,
                "hide_ankimcp_toolbar_indicator": False,
            }
        )
    )
    assert mgr.writes == []
