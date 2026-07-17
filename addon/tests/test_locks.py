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
    # Keep the module-level original-refs clean between tests.
    locks._original_write_config = None
    locks._original_about_debug_symbols.clear()
    yield
    locks._original_write_config = None
    locks._original_about_debug_symbols.clear()


# --------------------------------------------------------------------------- #
# Seam 1 — menu action + whole-menu locks
# --------------------------------------------------------------------------- #


def test_apply_menu_locks_hides_actions_and_keeps_file_menu(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({}))

    # default-locked actions are hidden (hide mode), not disabled
    assert form.actionPreferences.visible is False
    assert form.actionPreferences.enabled is True
    assert form.action_check_for_updates.visible is False
    # the v0.7.0 per-item File locks are hidden by default...
    assert form.actionImport.visible is False
    assert form.actionExport.visible is False
    assert form.action_create_backup.visible is False
    assert form.action_open_backup.visible is False
    assert form.actionExit.visible is False
    # ...so the File menu itself and Switch Profile stay usable by default
    # (multi-profile support): net result is a File menu with only Switch
    # Profile.
    assert form.menuCol.menuAction().visible is True
    assert form.actionSwitchProfile.visible is True
    # default-unlocked actions stay untouched
    assert form.actionNoteTypes.visible is True
    assert form.actionFullDatabaseCheck.visible is True


def test_apply_menu_locks_disable_mode_greys_out(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(
        core.merge_config({"hide_vs_disable": "disable", "lock_file_menu": True})
    )

    assert form.actionPreferences.enabled is False
    assert form.actionPreferences.visible is True
    assert form.menuCol.menuAction().enabled is False
    assert form.menuCol.menuAction().visible is True


def test_apply_menu_locks_file_menu_lock_still_gateable_on(monkeypatch):
    # The whole-menu gate stays implemented — only the default changed.
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({"lock_file_menu": True}))

    assert form.menuCol.menuAction().visible is False


def test_apply_menu_locks_gates_off_leave_targets_alone(monkeypatch):
    form = make_form_with_all_lock_targets()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(
        core.merge_config(
            {"lock_check_for_updates": False, "lock_export": False}
        )
    )

    assert form.action_check_for_updates.visible is True
    assert form.actionExport.visible is True


def test_apply_menu_locks_missing_attrs_warn_not_crash(monkeypatch, capsys):
    # A bare form (e.g. an older/newer Anki that renamed everything) must be a
    # logged skip for every locked attr — never an AttributeError crash.
    form = types.SimpleNamespace()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(form=form))

    locks.apply_menu_locks(core.merge_config({"lock_file_menu": True}))

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

    locks.apply_menu_locks(core.merge_config({"lock_file_menu": True}))

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

    locks.apply_menu_locks(core.merge_config({"lock_file_menu": True}))

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

    locks.apply_menu_locks(core.merge_config({"lock_file_menu": True}))

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
# Seam 9 — profile-manager screen lockdown
# --------------------------------------------------------------------------- #

#: The buttons ci-buddy must never touch on the profile-manager screen
#: (users manage their own profiles — product decision).
_PM_KEPT_BUTTONS = ("login", "add", "rename", "delete_2")


def make_profile_form():
    """A fake ``mw.profileForm`` carrying every profiles.ui button — the locked
    ones and the kept ones."""
    form = types.SimpleNamespace()
    for attr in core.PROFILE_MANAGER_LOCK_MAP.values():
        setattr(form, attr, FakeAction())
    for attr in _PM_KEPT_BUTTONS:
        setattr(form, attr, FakeAction())
    return form


class FakeProfileMw:
    """A fake ``mw`` whose ``showProfileManager`` rebuilds ``profileForm`` from
    scratch on every call — exactly what aqt's ``AnkiQt.showProfileManager``
    does (main.py assigns a fresh ``Ui_MainWindow`` each showing)."""

    def __init__(self):
        self.profileForm = None
        self.show_calls = 0

    def showProfileManager(self):
        self.show_calls += 1
        self.profileForm = make_profile_form()


def test_apply_profile_manager_locks_hides_dangerous_buttons(monkeypatch):
    form = make_profile_form()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=form))

    locks.apply_profile_manager_locks(core.merge_config({}))

    # dangerous buttons hidden (hide mode), not disabled
    assert form.openBackup.visible is False
    assert form.quit.visible is False
    assert form.downgrade_button.visible is False
    assert form.openBackup.enabled is True
    # profile-management buttons are never touched
    for attr in _PM_KEPT_BUTTONS:
        assert getattr(form, attr).visible is True
        assert getattr(form, attr).enabled is True


def test_apply_profile_manager_locks_disable_mode_greys_out(monkeypatch):
    form = make_profile_form()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=form))

    locks.apply_profile_manager_locks(
        core.merge_config({"hide_vs_disable": "disable"})
    )

    assert form.quit.enabled is False
    assert form.quit.visible is True


def test_apply_profile_manager_locks_gates_off_leave_buttons_alone(monkeypatch):
    form = make_profile_form()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=form))

    locks.apply_profile_manager_locks(
        core.merge_config({"lock_profile_manager_quit": False})
    )

    assert form.quit.visible is True
    assert form.openBackup.visible is False  # other gates still applied


def test_apply_profile_manager_locks_missing_button_warns_not_crash(
    monkeypatch, capsys
):
    # A future profiles.ui that renamed a button: logged skip, other locks
    # still applied — never an AttributeError crash.
    form = make_profile_form()
    del form.openBackup
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=form))

    locks.apply_profile_manager_locks(core.merge_config({}))

    out = capsys.readouterr().out
    assert "openBackup" in out
    assert "lock skipped" in out
    assert form.quit.visible is False  # remaining locks still applied


def test_apply_profile_manager_locks_reshaped_button_warns_and_continues(
    monkeypatch, capsys
):
    # A button attr that exists but is no longer button-shaped is a logged
    # skip too — per-item, so the remaining locks still apply.
    form = make_profile_form()
    form.quit = object()
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=form))

    locks.apply_profile_manager_locks(core.merge_config({}))

    out = capsys.readouterr().out
    assert "quit" in out
    assert "lock skipped" in out
    assert form.openBackup.visible is False
    assert form.downgrade_button.visible is False


def test_apply_profile_manager_locks_no_form_is_noop(monkeypatch):
    install_fake_aqt(monkeypatch, types.SimpleNamespace(profileForm=None))
    locks.apply_profile_manager_locks(core.merge_config({}))  # must not raise


def test_install_profile_manager_lock_covers_every_showing(monkeypatch):
    # aqt rebuilds the profile-manager form fresh on EVERY showProfileManager
    # call (main.py) — the wrap must lock the new buttons each time, both for
    # the startup picker (first call) and File → Switch Profile (later calls).
    mw = FakeProfileMw()
    install_fake_aqt(monkeypatch, mw)

    locks.install_profile_manager_lock(core.merge_config({}))

    # first showing (startup picker path)
    mw.showProfileManager()
    first_form = mw.profileForm
    assert mw.show_calls == 1  # original still ran
    assert first_form.openBackup.visible is False
    assert first_form.quit.visible is False
    assert first_form.downgrade_button.visible is False
    assert first_form.login.visible is True

    # second showing (File → Switch Profile) builds a FRESH form — the locks
    # must hold there too, or the seam silently unlocks.
    mw.showProfileManager()
    second_form = mw.profileForm
    assert second_form is not first_form
    assert second_form.openBackup.visible is False
    assert second_form.quit.visible is False
    assert second_form.downgrade_button.visible is False
    assert second_form.login.visible is True


def test_install_profile_manager_lock_is_idempotent(monkeypatch):
    mw = FakeProfileMw()
    install_fake_aqt(monkeypatch, mw)

    locks.install_profile_manager_lock(core.merge_config({}))
    wrapped = mw.showProfileManager
    # Second install must be a no-op: same wrapper, not a wrap-of-a-wrap.
    locks.install_profile_manager_lock(core.merge_config({}))
    assert mw.showProfileManager is wrapped

    mw.showProfileManager()
    assert mw.show_calls == 1  # original ran exactly once per call


def test_install_profile_manager_lock_missing_method_warns_not_crash(
    monkeypatch, capsys
):
    # A future aqt without showProfileManager: logged skip — the buttons then
    # stay visible (fail open), never a startup failure.
    install_fake_aqt(monkeypatch, types.SimpleNamespace())

    locks.install_profile_manager_lock(core.merge_config({}))

    out = capsys.readouterr().out
    assert "showProfileManager" in out
    assert "profile-manager lock skipped" in out


def test_install_profile_manager_lock_locks_preexisting_form(monkeypatch):
    # Defensive path: if a form already exists at install time it is locked
    # immediately, not only on the next showing.
    mw = FakeProfileMw()
    mw.profileForm = make_profile_form()
    install_fake_aqt(monkeypatch, mw)

    locks.install_profile_manager_lock(core.merge_config({}))

    assert mw.profileForm.openBackup.visible is False
    assert mw.profileForm.login.visible is True


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
    # what provisioners._real_write_config recovers to persist a sibling
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
# Seam 8 — sanitize About's "Copy Debug Info"
# --------------------------------------------------------------------------- #

_REAL_SUPPORT_TEXT = "Anki 26.05 Python 3.9 Qt 6.6 /home/user/.local/share/Anki2"
_REAL_ADDON_INFO = "Add-ons possibly involved: ankimcp"


def _install_fake_aqt_about(monkeypatch, *, with_addon_info=True):
    """Install fake ``aqt`` + ``aqt.about`` submodules so
    ``sanitize_about_debug_info`` (which does ``import aqt.about``) resolves
    against them. Mirrors the real layout: ``aqt.about`` holds its OWN bindings
    of ``supportText``/``addon_debug_info`` (imported at its top from
    ``aqt.utils``/``aqt.errors``)."""
    about_mod = types.ModuleType("aqt.about")
    about_mod.supportText = lambda: _REAL_SUPPORT_TEXT
    if with_addon_info:
        about_mod.addon_debug_info = lambda: _REAL_ADDON_INFO

    aqt_mod = types.ModuleType("aqt")
    aqt_mod.about = about_mod
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    monkeypatch.setitem(sys.modules, "aqt.about", about_mod)
    return about_mod


def _on_copy_text(about_mod, addons_dirty=True):
    """Reproduce what ``about.py``'s ``on_copy`` puts on the clipboard: it
    resolves ``supportText``/``addon_debug_info`` as *module globals of
    aqt.about* at click time (the whole point of the Seam 8 rebinding)."""
    txt = about_mod.supportText()
    if addons_dirty:
        txt += "\n" + about_mod.addon_debug_info()
    return txt


def test_sanitize_about_debug_info_copies_placeholder(monkeypatch):
    about_mod = _install_fake_aqt_about(monkeypatch)

    locks.sanitize_about_debug_info()

    # What the button now puts on the clipboard: placeholder body, empty
    # add-on suffix — no environment data.
    assert _on_copy_text(about_mod) == core.DEBUG_INFO_PLACEHOLDER + "\n"
    assert _on_copy_text(about_mod, addons_dirty=False) == core.DEBUG_INFO_PLACEHOLDER
    for leak in (_REAL_SUPPORT_TEXT, _REAL_ADDON_INFO):
        assert leak not in _on_copy_text(about_mod)

    # The originals are preserved (reversible/testable) and still functional —
    # e.g. aqt.errors keeps real debug text through its own bindings.
    originals = locks._original_about_debug_symbols
    assert originals["supportText"]() == _REAL_SUPPORT_TEXT
    assert originals["addon_debug_info"]() == _REAL_ADDON_INFO


def test_sanitize_about_debug_info_is_idempotent(monkeypatch):
    about_mod = _install_fake_aqt_about(monkeypatch)

    locks.sanitize_about_debug_info()
    shim = about_mod.supportText
    assert getattr(shim, locks._CI_BUDDY_SHIM_ATTR, False)

    # Second run must not wrap the shim again — same objects survive, and the
    # stashed originals are not clobbered with the shims themselves.
    locks.sanitize_about_debug_info()
    assert about_mod.supportText is shim
    assert (
        locks._original_about_debug_symbols["supportText"]()
        == _REAL_SUPPORT_TEXT
    )


def test_sanitize_about_debug_info_missing_symbol_warns(monkeypatch, capsys):
    # An aqt build without addon_debug_info: the other symbol is still
    # sanitized, the missing one is a logged skip — never an AttributeError.
    about_mod = _install_fake_aqt_about(monkeypatch, with_addon_info=False)

    locks.sanitize_about_debug_info()

    assert about_mod.supportText() == core.DEBUG_INFO_PLACEHOLDER
    out = capsys.readouterr().out
    assert "addon_debug_info" in out
    assert "sanitizing skipped" in out


def test_sanitize_about_debug_info_reshaped_symbol_warns(monkeypatch, capsys):
    # A symbol that exists but is no longer callable is a logged skip too —
    # and per-item: the other symbol is still sanitized.
    about_mod = _install_fake_aqt_about(monkeypatch)
    about_mod.supportText = "no longer a function"

    locks.sanitize_about_debug_info()

    out = capsys.readouterr().out
    assert "supportText" in out
    assert "sanitizing skipped" in out
    assert about_mod.supportText == "no longer a function"  # left untouched
    assert about_mod.addon_debug_info() == ""  # other symbol still sanitized


def test_sanitize_about_debug_info_missing_aqt_about_fails_open(
    monkeypatch, capsys
):
    # No aqt.about module at all (radically reshaped aqt): the _safe-wrapped
    # register path logs the failure and moves on — never a crash, and the
    # stock About (with real debug info) keeps working.
    aqt_mod = types.ModuleType("aqt")  # no ``about`` submodule, no __path__
    monkeypatch.setitem(sys.modules, "aqt", aqt_mod)
    monkeypatch.delitem(sys.modules, "aqt.about", raising=False)

    locks._safe(locks.sanitize_about_debug_info)()  # must not raise

    out = capsys.readouterr().out
    assert "sanitize_about_debug_info failed" in out


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
_REGISTER_BASE = {
    "lock_addon_config_writes": False,
    "lock_debug_console": False,
    "sanitize_copy_debug_info": False,
}


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


def test_register_installs_profile_manager_lock_at_load_time(monkeypatch):
    _install_fake_aqt_for_register(monkeypatch, FakePM())
    mw = sys.modules["aqt"].mw
    mw.profileForm = None

    def fake_show():
        mw.profileForm = make_profile_form()

    mw.showProfileManager = fake_show

    locks.register(core.merge_config(_REGISTER_BASE))

    # The wrap is installed immediately at register (add-on load) time — NOT
    # deferred to a hook: the startup picker (multiple profiles, none
    # auto-loaded) shows inside setupProfile, BEFORE main_window_did_init
    # fires, so a hook-time install would miss the very first showing.
    assert mw.showProfileManager is not fake_show
    mw.showProfileManager()
    assert mw.profileForm.openBackup.visible is False
    assert mw.profileForm.quit.visible is False
    assert mw.profileForm.downgrade_button.visible is False
    assert mw.profileForm.login.visible is True


def test_register_all_gates_off_leaves_profile_manager_alone(monkeypatch):
    _install_fake_aqt_for_register(monkeypatch, FakePM())
    mw = sys.modules["aqt"].mw

    def fake_show():
        pass

    mw.showProfileManager = fake_show

    locks.register(
        core.merge_config(
            dict(
                _REGISTER_BASE,
                lock_profile_manager_open_backup=False,
                lock_profile_manager_quit=False,
                lock_profile_manager_downgrade=False,
            )
        )
    )

    # every Seam-9 gate off → showProfileManager is not wrapped at all
    assert mw.showProfileManager is fake_show


def _add_fake_about_to_register_env(monkeypatch):
    """Graft a fake ``aqt.about`` onto the register-test fake ``aqt``."""
    about_mod = types.ModuleType("aqt.about")
    about_mod.supportText = lambda: _REAL_SUPPORT_TEXT
    about_mod.addon_debug_info = lambda: _REAL_ADDON_INFO
    sys.modules["aqt"].about = about_mod
    monkeypatch.setitem(sys.modules, "aqt.about", about_mod)
    return about_mod


def test_register_sanitizes_about_debug_info_at_load_time(monkeypatch):
    _install_fake_aqt_for_register(monkeypatch, FakePM())
    about_mod = _add_fake_about_to_register_env(monkeypatch)

    locks.register(
        core.merge_config(dict(_REGISTER_BASE, sanitize_copy_debug_info=True))
    )

    # Applied immediately at register (add-on load) time — NOT deferred to a
    # hook: there is no gui_hook for the About dialog, and the rebinding must
    # be live before the first Copy Debug Info click.
    assert _on_copy_text(about_mod) == core.DEBUG_INFO_PLACEHOLDER + "\n"


def test_register_gate_off_leaves_about_debug_info_alone(monkeypatch):
    _install_fake_aqt_for_register(monkeypatch, FakePM())
    about_mod = _add_fake_about_to_register_env(monkeypatch)

    locks.register(core.merge_config(_REGISTER_BASE))

    # Untouched original behaviour: the real debug text is still copied.
    assert _on_copy_text(about_mod) == _REAL_SUPPORT_TEXT + "\n" + _REAL_ADDON_INFO
    assert not getattr(about_mod.supportText, locks._CI_BUDDY_SHIM_ATTR, False)
