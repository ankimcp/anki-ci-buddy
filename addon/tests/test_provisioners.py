"""Unit tests for the config provisioners, driven with fake aqt/anki objects.

Covers the collection-config provisioner (RENDER_LATEX) and the sibling
add-on provisioner (hide AnkiMCP UI) — both run without a real Anki.
"""

import json
import os
import sys
import tempfile
import types

import pytest

from ci_buddy import core
from ci_buddy.provisioners import (
    AddonConfigProvisioner,
    CollectionConfigProvisioner,
    register,
)


@pytest.fixture
def logs():
    return []


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


def install_fake_gui_hooks(monkeypatch, mw=None):
    """Install a fake ``aqt`` exposing ``gui_hooks`` with recording hook lists
    and an optional ``mw``."""
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = mw if mw is not None else types.SimpleNamespace()
    gui_hooks = types.SimpleNamespace(
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
    register(core.merge_config({"ensure_latex_generation": True}))
    assert len(gui_hooks.collection_did_load.callbacks) == 1
    assert len(gui_hooks.sync_did_finish.callbacks) == 1


def test_register_skips_latex_hooks_when_flag_off(monkeypatch):
    gui_hooks = install_fake_gui_hooks(monkeypatch)
    register(core.merge_config({"ensure_latex_generation": False}))
    assert gui_hooks.collection_did_load.callbacks == []
    assert gui_hooks.sync_did_finish.callbacks == []


# --- hide AnkiMCP UI ----------------------------------------------------- #


class FakeAddonManager:
    """Stand-in for aqt's AddonManager.

    Backed by a real temp addons folder so the provisioner's on-disk
    ``manifest.json`` reading (``_installed_addon_packages``) runs against actual
    files, exercising the enumeration + directory-resolution path end-to-end.

    - ``configs`` maps a *directory* name → the dict ``getConfig`` returns.
    - ``packages`` maps a directory name → its manifest ``package`` field; a
      directory absent from ``packages`` defaults to ``package == dir_name`` (the
      dev/source install where the two coincide). A directory mapped to ``None``
      models an add-on with no ``package`` in its manifest.

    Exposes the enumeration API the provisioner uses: ``allAddons()`` (directory
    names) and ``addonsFolder(dir)`` (path holding that add-on's manifest.json).
    """

    def __init__(self, configs=None, packages=None):
        self._configs = dict(configs or {})
        packages = dict(packages or {})
        self.writes: list[tuple] = []
        self._tmp = tempfile.TemporaryDirectory()
        self._root = self._tmp.name
        for dir_name in self._configs:
            folder = os.path.join(self._root, dir_name)
            os.makedirs(folder, exist_ok=True)
            manifest = {"name": dir_name}
            package = packages.get(dir_name, dir_name)
            if package is not None:
                manifest["package"] = package
            with open(
                os.path.join(folder, "manifest.json"), "w", encoding="utf-8"
            ) as fh:
                json.dump(manifest, fh)

    def allAddons(self):
        return sorted(self._configs)

    def addonsFolder(self, module=None):
        return self._root if module is None else os.path.join(self._root, module)

    def getConfig(self, module):
        return self._configs.get(module)

    def writeConfig(self, module, conf):
        self.writes.append((module, conf))
        self._configs[module] = conf


def make_addon_provisioner(logs, **overrides):
    config = core.merge_config(overrides)
    return AddonConfigProvisioner(config, printer=logs.append)


def _ui_on(**extra):
    # both hide gates on by default (the shipped default); override per test
    base = {
        "hide_ankimcp_toolbar_indicator": True,
        "hide_ankimcp_settings_menu_item": True,
    }
    base.update(extra)
    return base


def test_hide_ui_forces_both_keys_in_one_write(logs):
    prov = make_addon_provisioner(logs, **_ui_on())
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
                "port": 8765,
            }
        }
    )

    assert prov.hide_ankimcp_ui(mgr) is True
    # a SINGLE writeConfig with both keys forced false, other keys preserved
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {
                "show_toolbar_indicator": False,
                "show_settings_menu_item": False,
                "port": 8765,
            },
        )
    ]
    log = next(l for l in logs if "hid AnkiMCP UI" in l)
    assert "show_toolbar_indicator=false" in log
    assert "show_settings_menu_item=false" in log


def test_hide_ui_forces_only_the_still_needed_key(logs):
    # toolbar indicator already false; only the menu item needs forcing
    prov = make_addon_provisioner(logs, **_ui_on())
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": False,
                "show_settings_menu_item": True,
            }
        }
    )

    assert prov.hide_ankimcp_ui(mgr) is True
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": False, "show_settings_menu_item": False},
        )
    ]
    log = next(l for l in logs if "hid AnkiMCP UI" in l)
    # log names only the key it actually flipped
    assert "show_settings_menu_item=false" in log
    assert "show_toolbar_indicator" not in log


def test_hide_ui_idempotent_when_both_already_false(logs):
    prov = make_addon_provisioner(logs, **_ui_on())
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": False,
                "show_settings_menu_item": False,
            }
        }
    )

    assert prov.hide_ankimcp_ui(mgr) is False
    assert mgr.writes == []  # never re-writes an already-hidden UI


def test_hide_ui_respects_toolbar_only_gate(logs):
    # only the toolbar gate on → menu-item key left untouched even though true
    prov = make_addon_provisioner(
        logs, **_ui_on(hide_ankimcp_settings_menu_item=False)
    )
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )

    assert prov.hide_ankimcp_ui(mgr) is True
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": False, "show_settings_menu_item": True},
        )
    ]


def test_hide_ui_respects_menu_item_only_gate(logs):
    # only the menu-item gate on → toolbar key left untouched even though true
    prov = make_addon_provisioner(
        logs, **_ui_on(hide_ankimcp_toolbar_indicator=False)
    )
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )

    assert prov.hide_ankimcp_ui(mgr) is True
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": True, "show_settings_menu_item": False},
        )
    ]


def test_hide_ui_resolves_dir_when_it_differs_from_package(logs):
    # THE REGRESSION: hosted image installs AnkiMCP under directory 'ankimcp'
    # while its manifest still declares package 'anki_mcp_server'. getConfig/
    # writeConfig are keyed by the DIRECTORY, so the write must target 'ankimcp'.
    prov = make_addon_provisioner(logs, **_ui_on())
    mgr = FakeAddonManager(
        configs={
            "ankimcp": {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        },
        packages={"ankimcp": core.ANKIMCP_PACKAGE},
    )

    assert prov.hide_ankimcp_ui(mgr) is True
    assert mgr.writes == [
        (
            "ankimcp",
            {"show_toolbar_indicator": False, "show_settings_menu_item": False},
        )
    ]
    assert any("hid AnkiMCP UI" in l for l in logs)


def test_hide_ui_noop_when_ankimcp_absent(logs):
    prov = make_addon_provisioner(logs, **_ui_on())
    # A sibling add-on is installed, but none declares the AnkiMCP package.
    mgr = FakeAddonManager(
        configs={"some_other_addon": {"foo": 1}},
        packages={"some_other_addon": "com.example.other"},
    )

    assert prov.hide_ankimcp_ui(mgr) is False
    assert mgr.writes == []
    # the silence that hid this bug for a release is now a greppable warning
    assert any("CI_BUDDY_ADDON_PACKAGE_NOT_FOUND" in l for l in logs)


def test_hide_ui_noop_when_no_manager(logs):
    prov = make_addon_provisioner(logs, **_ui_on())
    assert prov.hide_ankimcp_ui(None) is False


def test_installed_addon_packages_reads_manifest_and_is_defensive(tmp_path):
    # dir 'ankimcp' has a good manifest (package differs from dir); dir 'broken'
    # has malformed JSON; dir 'nomanifest' has none; dir 'listmanifest' is a JSON
    # array (no .get). All defensive cases must yield package None, never raise.
    from ci_buddy.provisioners import _installed_addon_packages

    (tmp_path / "ankimcp").mkdir()
    (tmp_path / "ankimcp" / "manifest.json").write_text(
        json.dumps({"name": "AnkiMCP", "package": "anki_mcp_server"}),
        encoding="utf-8",
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "nomanifest").mkdir()
    (tmp_path / "listmanifest").mkdir()
    (tmp_path / "listmanifest" / "manifest.json").write_text("[]", encoding="utf-8")

    mgr = types.SimpleNamespace(
        allAddons=lambda: ["ankimcp", "broken", "nomanifest", "listmanifest"],
        addonsFolder=lambda module=None: (
            str(tmp_path) if module is None else str(tmp_path / module)
        ),
    )

    pairs = dict(_installed_addon_packages(mgr))
    assert pairs["ankimcp"] == "anki_mcp_server"
    assert pairs["broken"] is None
    assert pairs["nomanifest"] is None
    assert pairs["listmanifest"] is None


def test_installed_addon_packages_empty_when_no_enumeration_api():
    from ci_buddy.provisioners import _installed_addon_packages

    # a manager without allAddons/addonsFolder → empty (fail-open, no crash)
    assert _installed_addon_packages(types.SimpleNamespace()) == []


def test_hide_ui_uses_original_writeconfig_through_shim(logs):
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

    # Real enumeration API (allAddons/addonsFolder/getConfig) via FakeAddonManager,
    # but writeConfig swapped for the Seam-4 dropping shim.
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )
    mgr.writeConfig = dropping_shim

    prov = make_addon_provisioner(logs, **_ui_on())
    assert prov.hide_ankimcp_ui(mgr) is True
    # persisted via the ORIGINAL, not dropped by the shim
    assert real_writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": False, "show_settings_menu_item": False},
        )
    ]


def test_real_write_config_falls_through_without_shim():
    mgr = FakeAddonManager()
    # no shim installed → the live writeConfig is already the real one
    from ci_buddy.provisioners import _real_write_config

    # (== not is: mgr.writeConfig yields a fresh bound-method wrapper each access)
    assert _real_write_config(mgr) == mgr.writeConfig


def test_register_hides_ui_at_load_time(monkeypatch):
    # register() must coerce the UI keys immediately (not on a hook), using the
    # addonManager on mw — both gated keys in one write.
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )
    install_fake_gui_hooks(
        monkeypatch, mw=types.SimpleNamespace(addonManager=mgr)
    )

    register(
        core.merge_config(
            {
                "ensure_latex_generation": False,
                "hide_ankimcp_toolbar_indicator": True,
                "hide_ankimcp_settings_menu_item": True,
            }
        )
    )
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": False, "show_settings_menu_item": False},
        )
    ]


def test_register_hides_ui_when_only_one_gate_on(monkeypatch):
    # a single gate is enough to run the coercion (menu-item only here)
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )
    install_fake_gui_hooks(
        monkeypatch, mw=types.SimpleNamespace(addonManager=mgr)
    )

    register(
        core.merge_config(
            {
                "ensure_latex_generation": False,
                "hide_ankimcp_toolbar_indicator": False,
                "hide_ankimcp_settings_menu_item": True,
            }
        )
    )
    assert mgr.writes == [
        (
            core.ANKIMCP_PACKAGE,
            {"show_toolbar_indicator": True, "show_settings_menu_item": False},
        )
    ]


def test_register_skips_ui_when_both_gates_off(monkeypatch):
    mgr = FakeAddonManager(
        {
            core.ANKIMCP_PACKAGE: {
                "show_toolbar_indicator": True,
                "show_settings_menu_item": True,
            }
        }
    )
    install_fake_gui_hooks(
        monkeypatch, mw=types.SimpleNamespace(addonManager=mgr)
    )

    register(
        core.merge_config(
            {
                "ensure_latex_generation": False,
                "hide_ankimcp_toolbar_indicator": False,
                "hide_ankimcp_settings_menu_item": False,
            }
        )
    )
    assert mgr.writes == []
