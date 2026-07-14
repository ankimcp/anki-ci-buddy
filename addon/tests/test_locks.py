"""Unit tests for the Part A lock seams that don't need a running Anki.

``ci_buddy.locks`` imports ``aqt`` lazily (inside handlers), so the module
imports cleanly here. Handlers that touch ``mw`` get a fake ``aqt`` injected via
sys.modules; the pure filter handler is called directly.
"""

import sys
import types

import pytest

from ci_buddy import core, locks


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeAddonManager:
    def __init__(self):
        self.written: list[tuple] = []

    def writeConfig(self, module, conf):
        self.written.append((module, conf))


class FakeDialog:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, value):
        self.enabled = value


def install_fake_aqt(monkeypatch, mw):
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = mw
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    return mw


@pytest.fixture(autouse=True)
def _reset_shim_ref():
    # Keep the module-level original-ref clean between tests.
    locks._original_write_config = None
    yield
    locks._original_write_config = None


# --------------------------------------------------------------------------- #
# Seam 4 — writeConfig shim
# --------------------------------------------------------------------------- #


def test_write_config_shim_drops_writes(monkeypatch):
    mgr = FakeAddonManager()
    original = mgr.writeConfig
    mw = types.SimpleNamespace(addonManager=mgr)
    install_fake_aqt(monkeypatch, mw)

    locks.install_write_config_shim()

    # A write through the shimmed manager is silently dropped (not delegated).
    mgr.writeConfig("some.module", {"x": 1})
    assert mgr.written == []

    # The original reference is preserved and still functional.
    assert locks._original_write_config == original
    locks._original_write_config("some.module", {"y": 2})
    assert mgr.written == [("some.module", {"y": 2})]

    # Producer side of the cross-module contract: the shim must expose the
    # genuine original on itself under core.ORIGINAL_WRITE_CONFIG_ATTR — this is
    # what provisioning._real_write_config recovers to persist a sibling
    # add-on's config despite the write lock. Calling the exposed original must
    # actually persist (not drop) a write.
    exposed = getattr(mgr.writeConfig, core.ORIGINAL_WRITE_CONFIG_ATTR, None)
    assert exposed == original
    exposed("other.module", {"z": 3})
    assert ("other.module", {"z": 3}) in mgr.written


def test_write_config_shim_is_idempotent(monkeypatch):
    mgr = FakeAddonManager()
    mw = types.SimpleNamespace(addonManager=mgr)
    install_fake_aqt(monkeypatch, mw)

    locks.install_write_config_shim()
    shim = mgr.writeConfig
    first_original = locks._original_write_config

    # Second install must be a no-op: same shim, original ref not overwritten
    # with the shim itself.
    locks.install_write_config_shim()
    assert mgr.writeConfig is shim
    assert locks._original_write_config == first_original

    mgr.writeConfig("m", {"a": 1})
    assert mgr.written == []  # still dropping writes


def test_write_config_shim_no_manager(monkeypatch):
    # No addonManager yet → no-op, no crash.
    mw = types.SimpleNamespace(addonManager=None)
    install_fake_aqt(monkeypatch, mw)
    locks.install_write_config_shim()
    assert locks._original_write_config is None


# --------------------------------------------------------------------------- #
# Seam 2 — dialog filter
# --------------------------------------------------------------------------- #


def test_dialog_filter_disables_locked_names():
    config = core.merge_config({"lock_preferences": True, "lock_addons": True})
    locked = core.locked_dialog_names(config)

    for name in ("Preferences", "AddonsDialog"):
        dialog = FakeDialog()
        locks.on_dialog_opened(object(), name, dialog, locked)
        assert dialog.enabled is False, name


def test_dialog_filter_leaves_editing_dialogs_enabled():
    config = core.merge_config({"lock_preferences": True, "lock_addons": True})
    locked = core.locked_dialog_names(config)

    for name in ("AddCards", "Browser", "EditCurrent", "DeckStats"):
        dialog = FakeDialog()
        locks.on_dialog_opened(object(), name, dialog, locked)
        assert dialog.enabled is True, name


def test_dialog_filter_respects_config_gating():
    # Only Add-ons locked → Preferences dialog is left enabled.
    config = core.merge_config(
        {"lock_preferences": False, "lock_addons": True}
    )
    locked = core.locked_dialog_names(config)

    prefs = FakeDialog()
    locks.on_dialog_opened(object(), "Preferences", prefs, locked)
    assert prefs.enabled is True

    addons = FakeDialog()
    locks.on_dialog_opened(object(), "AddonsDialog", addons, locked)
    assert addons.enabled is False


# --------------------------------------------------------------------------- #
# Seam 6 — debug console
# --------------------------------------------------------------------------- #


def _install_fake_aqt_console(monkeypatch, real_console, *, with_legacy=False):
    """Install fake ``aqt`` + ``aqt.debug_console`` + ``aqt.main`` submodules so
    ``neuter_debug_console_callable`` (which does ``import aqt.debug_console`` /
    ``import aqt.main``) resolves against them."""
    dbg_mod = types.ModuleType("aqt.debug_console")
    dbg_mod.show_debug_console = real_console

    main_mod = types.ModuleType("aqt.main")
    main_mod.show_debug_console = real_console
    if with_legacy:

        class AnkiQt:
            def onDebug(self, *a, **k):
                real_console()

        main_mod.AnkiQt = AnkiQt

    aqt_mod = types.ModuleType("aqt")
    aqt_mod.debug_console = dbg_mod
    aqt_mod.main = main_mod

    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    monkeypatch.setitem(sys.modules, "aqt.debug_console", dbg_mod)
    monkeypatch.setitem(sys.modules, "aqt.main", main_mod)
    return dbg_mod, main_mod


def test_neuter_debug_console_callable_blocks_repl(monkeypatch):
    opened: list[str] = []

    def real_console(*a, **k):
        opened.append("opened")

    dbg_mod, main_mod = _install_fake_aqt_console(
        monkeypatch, real_console, with_legacy=True
    )

    locks.neuter_debug_console_callable()

    # Both module names now point at the ci-buddy shim (marked idempotent).
    assert getattr(dbg_mod.show_debug_console, locks._CI_BUDDY_SHIM_ATTR, False)
    assert getattr(main_mod.show_debug_console, locks._CI_BUDDY_SHIM_ATTR, False)

    # Invoking either replacement is a no-op — the real console never runs.
    dbg_mod.show_debug_console()
    main_mod.show_debug_console()
    # Legacy method form is neutered too.
    main_mod.AnkiQt().onDebug()
    assert opened == []


def test_neuter_debug_console_callable_is_idempotent(monkeypatch):
    def real_console(*a, **k):
        pass

    dbg_mod, main_mod = _install_fake_aqt_console(monkeypatch, real_console)

    locks.neuter_debug_console_callable()
    shim = dbg_mod.show_debug_console
    # Second run must not wrap the shim again — same object survives.
    locks.neuter_debug_console_callable()
    assert dbg_mod.show_debug_console is shim


class _FakeKeySequence:
    def __init__(self, spec):
        self.spec = spec

    def __eq__(self, other):
        return isinstance(other, _FakeKeySequence) and self.spec == other.spec


class _FakeShortcut:
    def __init__(self, spec):
        self._key = _FakeKeySequence(spec)
        self.enabled = True

    def key(self):
        return self._key

    def setEnabled(self, value):
        self.enabled = value


def test_disable_debug_console_shortcut_targets_only_console_keys(monkeypatch):
    debug_sc = _FakeShortcut("Ctrl+:")
    other_sc = _FakeShortcut("Ctrl+F")

    class FakeMw:
        def findChildren(self, _cls):
            return [debug_sc, other_sc]

    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = FakeMw()
    qt_mod = types.ModuleType("aqt.qt")
    qt_mod.QKeySequence = _FakeKeySequence
    qt_mod.QShortcut = _FakeShortcut
    aqt_mod.qt = qt_mod
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    monkeypatch.setitem(sys.modules, "aqt.qt", qt_mod)

    locks.disable_debug_console_shortcut()

    assert debug_sc.enabled is False  # console shortcut disabled
    assert other_sc.enabled is True  # unrelated shortcut untouched
