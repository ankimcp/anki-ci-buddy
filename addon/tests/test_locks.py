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


class FakeAction:
    def __init__(self):
        self.visible = True
        self.enabled = True

    def setVisible(self, value):
        self.visible = value

    def setEnabled(self, value):
        self.enabled = value


class FakeMenu:
    """A QMenu stand-in: locked through ``menuAction()``, like the real thing."""

    def __init__(self):
        self._menu_action = FakeAction()

    def menuAction(self):
        return self._menu_action


class FakePM:
    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    def set_update_check(self, on):
        self.calls.append(("set_update_check", on))

    def set_check_for_addon_updates(self, on):
        self.calls.append(("set_check_for_addon_updates", on))


def install_fake_aqt(monkeypatch, mw):
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.mw = mw
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    return mw


def make_form_with_all_lock_targets():
    """A fake ``mw.form`` carrying every action/menu attr in the lock maps."""
    form = types.SimpleNamespace()
    for attr in core.LOCK_ACTION_MAP.values():
        setattr(form, attr, FakeAction())
    for attr in core.LOCK_MENU_MAP.values():
        setattr(form, attr, FakeMenu())
    return form


@pytest.fixture(autouse=True)
def _reset_shim_ref():
    # Keep the module-level original-ref clean between tests.
    locks._original_write_config = None
    yield
    locks._original_write_config = None


# --------------------------------------------------------------------------- #
# Seam 1 — menu action + whole-menu locks
# --------------------------------------------------------------------------- #


def test_apply_menu_locks_hides_actions_and_file_menu(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    # default-locked actions are hidden (hide mode), not disabled
    assert form.actionPreferences.visible is False
    assert form.actionPreferences.enabled is True
    assert form.action_check_for_updates.visible is False
    # the File menu is hidden via its menuAction (a QMenu is not a QAction)
    assert form.menuCol.menuAction().visible is False
    # default-unlocked actions stay untouched
    assert form.actionNoteTypes.visible is True
    assert form.actionFullDatabaseCheck.visible is True


def test_apply_menu_locks_disable_mode_greys_out(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({"hide_vs_disable": "disable"}))

    assert form.actionPreferences.enabled is False
    assert form.actionPreferences.visible is True
    assert form.menuCol.menuAction().enabled is False
    assert form.menuCol.menuAction().visible is True


def test_apply_menu_locks_gates_off_leave_targets_alone(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(
        core.merge_config(
            {"lock_check_for_updates": False, "lock_file_menu": False}
        )
    )

    assert form.action_check_for_updates.visible is True
    assert form.menuCol.menuAction().visible is True


def test_apply_menu_locks_missing_attrs_warn_not_crash(monkeypatch, capsys):
    # A bare form (e.g. an older/newer Anki that renamed everything) must be a
    # logged skip for every locked attr — never an AttributeError crash.
    form = types.SimpleNamespace()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    out = capsys.readouterr().out
    # e.g. action_check_for_updates only exists on 26.05+ — warned, skipped
    assert "action_check_for_updates" in out
    assert "menuCol" in out
    assert "lock skipped" in out


def test_apply_menu_locks_menu_without_menu_action_warns(monkeypatch, capsys):
    # A menuCol attr that is not menu-shaped (no callable menuAction) is a
    # logged skip too — the guard covers renamed AND reshaped attrs.
    form = make_form_with_all_lock_targets()
    form.menuCol = object()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    assert "menuCol" in capsys.readouterr().out


def test_apply_menu_locks_reshaped_action_warns_and_continues(
    monkeypatch, capsys
):
    # An action attr that exists but is not action-shaped (no callable
    # setVisible/setEnabled) is a logged skip too — and per-item: it must
    # never abort the loop, so every remaining action lock and the whole-menu
    # lock still apply.
    form = make_form_with_all_lock_targets()
    form.actionPreferences = object()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    out = capsys.readouterr().out
    assert "actionPreferences" in out
    assert "lock skipped" in out
    # downstream locks in the maps were still applied
    assert form.actionImport.visible is False
    assert form.menuCol.menuAction().visible is False


def test_apply_menu_locks_menu_action_not_action_shaped_warns(
    monkeypatch, capsys
):
    # A menu whose menuAction() is callable but returns a non-action must be
    # a logged skip too — same _action_shaped guard as the action path — and
    # per-item: the remaining action locks still apply.
    class WeirdMenu:
        def menuAction(self):
            return object()

    form = make_form_with_all_lock_targets()
    form.menuCol = WeirdMenu()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    out = capsys.readouterr().out
    assert "menuCol" in out
    assert "lock skipped" in out
    # other locks were still applied
    assert form.actionPreferences.visible is False
    assert form.actionImport.visible is False


def test_apply_menu_locks_no_form_is_noop(monkeypatch):
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=None))
    locks.apply_menu_locks(core.merge_config({}))  # must not raise


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


# --------------------------------------------------------------------------- #
# Seam 7 — suppress automatic update checks
# --------------------------------------------------------------------------- #


def test_disable_automatic_update_checks_forces_both_flags_off(monkeypatch):
    pm = FakePM()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(pm=pm))

    locks.disable_automatic_update_checks()

    assert pm.calls == [
        ("set_update_check", False),
        ("set_check_for_addon_updates", False),
    ]


def test_disable_automatic_update_checks_missing_setter_warns(
    monkeypatch, capsys
):
    # An aqt build that renamed one setter: the other is still applied, the
    # missing one is a logged skip — never an AttributeError crash.
    class PartialPM:
        def __init__(self):
            self.calls = []

        def set_update_check(self, on):
            self.calls.append(("set_update_check", on))

    pm = PartialPM()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(pm=pm))

    locks.disable_automatic_update_checks()

    assert pm.calls == [("set_update_check", False)]
    assert "set_check_for_addon_updates" in capsys.readouterr().out


def test_disable_automatic_update_checks_no_pm_warns_not_crashes(
    monkeypatch, capsys
):
    install_fake_aqt(monkeypatch, types.SimpleNamespace(pm=None))
    locks.disable_automatic_update_checks()  # must not raise
    assert "mw.pm unavailable" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# register — Seam 7 gating and load-time application
# --------------------------------------------------------------------------- #


class _FakeHook:
    def __init__(self):
        self.handlers = []

    def append(self, fn):
        self.handlers.append(fn)


def _install_fake_aqt_for_register(monkeypatch, pm):
    """Fake ``aqt`` with the gui_hooks + mw surface ``locks.register`` touches
    (the debug-console and writeConfig seams are gated off by the caller)."""
    hooks = types.SimpleNamespace(
        main_window_did_init=_FakeHook(),
        dialog_manager_did_open_dialog=_FakeHook(),
        addons_dialog_will_show=_FakeHook(),
        top_toolbar_did_init_links=_FakeHook(),
        profile_did_open=_FakeHook(),
    )
    aqt_mod = types.ModuleType("aqt")
    aqt_mod.gui_hooks = hooks
    aqt_mod.mw = types.SimpleNamespace(pm=pm, addonManager=None, form=None)
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    return hooks


#: Gates off the register seams that would need extra fake modules, leaving the
#: Seam 7 behaviour under test isolated.
_REGISTER_BASE = {"lock_addon_config_writes": False, "lock_debug_console": False}


def test_register_suppresses_update_checks_at_load_time(monkeypatch):
    pm = FakePM()
    _install_fake_aqt_for_register(monkeypatch, pm)

    locks.register(core.merge_config(_REGISTER_BASE))

    # Applied immediately at register (add-on load) time — NOT deferred to a
    # hook: the automatic checks fire inside loadProfile, which runs before
    # main_window_did_init, so a hook-time force would lose the race.
    assert ("set_update_check", False) in pm.calls
    assert ("set_check_for_addon_updates", False) in pm.calls


def test_register_gate_off_leaves_update_checks_alone(monkeypatch):
    pm = FakePM()
    _install_fake_aqt_for_register(monkeypatch, pm)

    locks.register(
        core.merge_config(dict(_REGISTER_BASE, disable_update_checks=False))
    )

    assert pm.calls == []
