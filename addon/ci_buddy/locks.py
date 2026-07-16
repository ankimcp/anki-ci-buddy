"""Part A — GUI lock.

Turns Anki into a locked appliance using public ``aqt`` GUI hooks plus a
``writeConfig`` monkeypatch (spec §A.2). Collection editing (Add / Browse /
Study / Decks / Stats / **Manage Note Types**) stays fully usable.

MUST NOT import ``provisioning`` — the two modules are deliberately decoupled.

Every hook handler is wrapped so no exception can crash startup or block the
collection ("fail open for the collection", spec §5).

``aqt`` is imported **lazily** inside the handlers (mirroring provisioning.py) so
``import ci_buddy.locks`` works without a running Anki and the seam logic is
unit-testable directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from . import core

if TYPE_CHECKING:  # hints only — never imported at runtime
    from aqt.qt import QWidget


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a hook handler so any exception is logged, never propagated."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail open by design
            print(f"[ci_buddy] lock handler {fn.__name__} failed: {exc!r}")
        return None

    return wrapper


# --------------------------------------------------------------------------- #
# Seam 1 — disable menu actions
# --------------------------------------------------------------------------- #


def _lock_action(action: Any, hide: bool) -> None:
    """Hide (``setVisible(False)``) or grey out (``setEnabled(False)``) a
    QAction, per the ``hide_vs_disable`` decision already made in core."""
    if hide:
        action.setVisible(False)
    else:
        action.setEnabled(False)


def _action_shaped(action: Any) -> bool:
    """True when ``action`` can be locked by ``_lock_action`` — i.e. it is
    QAction-shaped (callable ``setVisible``/``setEnabled``). Guards against an
    attr that exists but was reshaped into something else on a newer Anki."""
    return callable(getattr(action, "setVisible", None)) and callable(
        getattr(action, "setEnabled", None)
    )


def apply_menu_locks(config: dict[str, Any]) -> None:
    """Hide/disable the locked menu actions and whole menus on ``mw.form``
    (Seam 1).

    Prefer disabling the menu *action* over the shown dialog: nothing gets
    constructed and there is no visible flash. Every lock is applied
    independently: a missing/renamed attr, or one that exists but is no longer
    action/menu-shaped, on ANY Anki version is skipped with a logged warning —
    it never aborts the remaining locks or crashes with an ``AttributeError``
    (e.g. ``action_check_for_updates`` only exists on 26.05+,
    ``action_upgrade_downgrade`` only on 25.07+).

    Whole menus (``core.LOCK_MENU_MAP``, e.g. the File menu ``menuCol``) are a
    QMenu, not a QAction, so they are locked through ``menu.menuAction()`` —
    the standard Qt way to hide/grey a menu-bar menu.
    """
    from aqt import mw  # lazy — keeps this module import-safe for tests

    form = getattr(mw, "form", None)
    if form is None:
        return

    hide = core.hide_actions(config)
    for attr, should_lock in core.action_lock_plan(config).items():
        if not should_lock:
            continue
        action = getattr(form, attr, None)
        if not _action_shaped(action):
            print(
                f"[ci_buddy] warning: menu action {attr!r} not present or not "
                "action-shaped on this Anki version (lock skipped)"
            )
            continue
        _lock_action(action, hide)

    for attr, should_lock in core.menu_lock_plan(config).items():
        if not should_lock:
            continue
        menu = getattr(form, attr, None)
        menu_action_getter = getattr(menu, "menuAction", None)
        menu_action = menu_action_getter() if callable(menu_action_getter) else None
        if not _action_shaped(menu_action):
            print(
                f"[ci_buddy] warning: menu {attr!r} not present or not "
                "menu-shaped on this Anki version (lock skipped)"
            )
            continue
        _lock_action(menu_action, hide)


# --------------------------------------------------------------------------- #
# Seam 2 — filtered dialog guard (belt-and-suspenders)
# --------------------------------------------------------------------------- #


def on_dialog_opened(
    _dialog_manager: Any,
    dialog_name: str,
    dialog_instance: "QWidget",
    locked_names: set[str],
) -> None:
    """Disable *only* the config-locked dialog names after a programmatic open.

    ``locked_names`` is derived from config (``core.locked_dialog_names``): it
    contains ``"Preferences"`` iff ``lock_preferences`` and ``"AddonsDialog"``
    iff ``lock_addons`` — so the guard is config-gated, not hardcoded.

    CRITICAL: exact-name match only. The same registry serves AddCards,
    Browser, EditCurrent, FilteredDeckConfigDialog, DeckStats — an unfiltered
    ``setEnabled(False)`` would break the collection-editing UI we must keep
    working (spec §A.3 Seam 2).
    """
    if dialog_name in locked_names and dialog_instance is not None:
        dialog_instance.setEnabled(False)


# --------------------------------------------------------------------------- #
# Seam 3 — grey out the whole Add-ons dialog
# --------------------------------------------------------------------------- #


def on_addons_dialog_will_show(dialog: "QWidget", config: dict[str, Any]) -> None:
    """Grey the entire Add-ons dialog before it shows (the NixOS lock seam)."""
    dialog.setEnabled(False)
    label = config.get("managed_by_label", core.DEFAULT_MANAGED_BY_LABEL)
    try:
        dialog.setWindowTitle(str(label))
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


# --------------------------------------------------------------------------- #
# Seam 4 — neuter add-on config persistence (writeConfig shim)
# --------------------------------------------------------------------------- #

#: Reference to the original ``writeConfig`` (so the shim is reversible/testable
#: and never lost). Populated by ``install_write_config_shim``.
_original_write_config: Callable[..., Any] | None = None


def install_write_config_shim() -> None:
    """Replace ``mw.addonManager.writeConfig`` with a wrap-and-delegate shim
    that does NOT call through — writes are silently dropped (spec §A.3 Seam 4).

    Reads (``getConfig``) are untouched, so add-ons still read their config;
    only the durable write to ``meta.json`` is blocked. Idempotent.
    """
    global _original_write_config

    from aqt import mw  # lazy — keeps this module import-safe for tests

    manager = getattr(mw, "addonManager", None)
    if manager is None:
        return
    if getattr(manager.writeConfig, "_ci_buddy_shim", False):
        return  # already installed

    _original_write_config = manager.writeConfig

    def _blocked_write_config(module: str, conf: dict) -> None:
        # Intentionally does not delegate to the original — drops the write.
        print(f"[ci_buddy] blocked writeConfig for {module!r} (managed environment)")

    _blocked_write_config._ci_buddy_shim = True  # type: ignore[attr-defined]
    # Stash the genuine original ON the shim callable as well as in the module
    # global. This is the decoupled contract provisioning.py relies on: it must
    # durably write ANOTHER add-on's config (AnkiMCP's UI config keys)
    # even after this shim is in place, and it recovers the real writeConfig via
    # this attribute WITHOUT importing locks (the two modules stay decoupled).
    # The attribute name lives in core.ORIGINAL_WRITE_CONFIG_ATTR — the single
    # source of truth shared with the consumer (provisioning._real_write_config).
    setattr(
        _blocked_write_config,
        core.ORIGINAL_WRITE_CONFIG_ATTR,
        _original_write_config,
    )
    manager.writeConfig = _blocked_write_config  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Seam 5 (config-gated) — sync surfaces
# --------------------------------------------------------------------------- #


def on_top_toolbar_did_init_links(links: list[str], _toolbar: Any) -> None:
    """Strip the top-toolbar *sync* link (Seam 5, config-gated).

    Filters the single link whose id is ``sync``; leaves every other link
    (including the sync spinner markup nested inside, which is a different id).
    Mutates ``links`` in place, as the hook contract requires.
    """
    links[:] = [link for link in links if not core.is_sync_toolbar_link(link)]


def disable_native_auto_sync() -> None:
    """Turn off Anki's auto-sync-on-open/close for the current profile.

    Anki gates open/close auto-sync on ``pm.auto_syncing_enabled()`` which reads
    ``profile["autoSync"]`` (verified in aqt/profiles.py + main.py
    ``can_auto_sync``). There is no public setter, so we set the in-memory
    profile flag. Applied per ``profile_did_open``; not persisted, so it never
    dirties ``prefs21.db`` on disk.

    Suppression is reliable from the *first* sync: in ``main.loadProfile`` the
    ``profile_did_open`` hook fires immediately before
    ``maybe_auto_sync_on_open_close``, and ``can_auto_sync`` reads
    ``pm.auto_syncing_enabled()`` (i.e. ``profile["autoSync"]``) live — so the
    flag we set here is already in effect for that very check.
    """
    from aqt import mw  # lazy — keeps this module import-safe for tests

    pm = getattr(mw, "pm", None)
    if pm is None or getattr(pm, "profile", None) is None:
        return
    pm.profile["autoSync"] = False


# --------------------------------------------------------------------------- #
# Seam 6 (config-gated) — disable the built-in Python debug console
# --------------------------------------------------------------------------- #

#: Key sequences that open Anki's in-process Python REPL. Anki binds ``Ctrl+:``
#: (which is ``Ctrl+Shift+;`` on a US layout — ``:`` is Shift+``;``) to
#: ``show_debug_console`` in ``AnkiQt.setupKeys`` (verified against the aqt
#: source for 25.09.x and 26.05). ``Ctrl+Shift+;`` is kept as a defensive extra.
_DEBUG_CONSOLE_KEYS: tuple[str, ...] = ("Ctrl+:", "Ctrl+Shift+;")

#: Marker set on our replacement callable so the neuter is idempotent.
_CI_BUDDY_SHIM_ATTR = "_ci_buddy_shim"


def neuter_debug_console_callable() -> None:
    """No-op the debug-console entry point so the REPL can never open, whatever
    surface (shortcut, menu, add-on) tries to invoke it.

    Anki 25.09+/26.05 dropped the old ``AnkiQt.onDebug`` method: the console is
    now the free function ``aqt.debug_console.show_debug_console``, imported into
    ``aqt.main`` and bound into a ``QShortcut`` at ``setupKeys`` time. We replace
    the name in BOTH modules — ``aqt.debug_console`` is the canonical definition,
    ``aqt.main`` is what a (re-run) ``setupKeys`` resolves. For older builds that
    still expose ``AnkiQt.onDebug`` we neuter that too. Idempotent.
    """
    import aqt.debug_console
    import aqt.main

    def _blocked_debug_console(*_a: Any, **_k: Any) -> None:
        # Intentionally does nothing — the managed appliance has no REPL.
        print("[ci_buddy] blocked debug console (managed environment)")

    setattr(_blocked_debug_console, _CI_BUDDY_SHIM_ATTR, True)

    for module in (aqt.debug_console, aqt.main):
        existing = getattr(module, "show_debug_console", None)
        if existing is None:
            continue
        if getattr(existing, _CI_BUDDY_SHIM_ATTR, False):
            continue  # already neutered
        module.show_debug_console = _blocked_debug_console  # type: ignore[attr-defined]

    # Legacy (Anki <= 24.x): neuter the old method form if it is still present.
    anki_qt = getattr(aqt.main, "AnkiQt", None)
    if anki_qt is not None and hasattr(anki_qt, "onDebug"):
        anki_qt.onDebug = lambda self, *a, **k: None  # type: ignore[assignment]


def disable_debug_console_shortcut() -> None:
    """Disable any already-built ``QShortcut`` on the main window whose key
    sequence opens the debug console.

    This is the RELIABLE seam on 25.09+/26.05: ``setupKeys`` has already run by
    ``main_window_did_init``, so a ``QShortcut`` created with ``activated`` bound
    to the ORIGINAL ``show_debug_console`` reference already exists on ``mw`` —
    re-binding the module name afterwards can't reach that captured reference, so
    we disable the widget itself.

    Fails OPEN (never crashes Anki / bricks the pod) but LOUDLY: if the disable
    throws, the Ctrl+: Python REPL — an arbitrary-code-execution surface reachable
    over noVNC — may stay live, so we emit a distinctive, greppable SECURITY line
    (stable token ``CI_BUDDY_DEBUG_CONSOLE_LOCK_FAILED`` for log-monitor alerting)
    instead of leaning on the generic ``_safe`` print, then re-swallow.
    """
    try:
        from aqt import mw  # lazy — keeps this module import-safe for tests
        from aqt.qt import QKeySequence, QShortcut

        targets = [QKeySequence(key) for key in _DEBUG_CONSOLE_KEYS]
        for shortcut in mw.findChildren(QShortcut):
            try:
                key = shortcut.key()
            except Exception:  # noqa: BLE001 — skip anything that won't report a key
                continue
            if any(key == target for target in targets):
                shortcut.setEnabled(False)
    except Exception as exc:  # noqa: BLE001 — fail OPEN, but LOUD (see token below)
        print(
            "[ci_buddy] SECURITY WARNING: CI_BUDDY_DEBUG_CONSOLE_LOCK_FAILED — "
            f"could not disable the debug-console shortcut ({exc!r}); the Ctrl+: "
            "Python REPL may be reachable over noVNC"
        )


# --------------------------------------------------------------------------- #
# Seam 7 (config-gated) — suppress automatic update checks
# --------------------------------------------------------------------------- #


def disable_automatic_update_checks() -> None:
    """Force the global app-update and add-on-update checks off via ``mw.pm``
    (Seam 7).

    Calls each setter in ``core.UPDATE_CHECK_SETTERS`` with ``False``:
    ``pm.set_update_check(False)`` suppresses the "new version released"
    prompt (``setup_auto_update`` → ``aqt.update.check_for_update``) and
    ``pm.set_check_for_addon_updates(False)`` suppresses the 24h-throttled
    add-on update fetch. Both live in pm.meta (global, prefs21.db), which is
    loaded before add-ons import — so the values are already in force for this
    launch's checks. Re-applied every launch, so a user re-enabling the checks
    in Preferences only lasts until the next boot.

    Version-safe: a missing ``pm`` or an absent/renamed setter is skipped with
    a logged warning — never an ``AttributeError`` crash.
    """
    from aqt import mw  # lazy — keeps this module import-safe for tests

    pm = getattr(mw, "pm", None)
    if pm is None:
        print(
            "[ci_buddy] warning: mw.pm unavailable; cannot suppress automatic "
            "update checks"
        )
        return
    for setter_name in core.UPDATE_CHECK_SETTERS:
        setter = getattr(pm, setter_name, None)
        if not callable(setter):
            print(
                f"[ci_buddy] warning: pm.{setter_name} not present on this "
                "Anki version (update-check suppression skipped)"
            )
            continue
        setter(False)


# --------------------------------------------------------------------------- #
# Seam 8 (config-gated) — sanitize About's "Copy Debug Info"
# --------------------------------------------------------------------------- #

#: Originals of the ``aqt.about`` symbols Seam 8 rebinds, keyed by symbol name
#: (so the sanitizing is reversible/testable and the originals are never lost).
#: Populated by ``sanitize_about_debug_info``; mirrors ``_original_write_config``.
_original_about_debug_symbols: dict[str, Callable[..., Any]] = {}


def sanitize_about_debug_info() -> None:
    """Rebind the debug-info sources inside ``aqt.about`` so the About dialog's
    "Copy Debug Info" button copies a harmless placeholder instead of the real
    ``supportText()`` + installed-add-on list (an environment-data leak in the
    managed deployment). The dialog itself — credits, AGPL notice, the button —
    stays completely stock; only the copied *text* changes.

    INTERCEPTION (verified against the 26.05 source, ``qt/aqt/about.py``):
    ``about.show`` builds the dialog fresh on every open, and its ``on_copy``
    closure calls ``supportText()`` / ``addon_debug_info()`` — names ``show``
    never binds locally, so Python resolves them as *module globals of
    ``aqt.about``* at CLICK time. Rebinding those two attributes therefore
    changes what lands on the clipboard without touching the dialog, buttons or
    signals — and it works identically however About is opened. (The obvious
    alternative — wrapping ``aqt.about.show`` — has a trap: Help > About goes
    ``mw.onAbout`` → ``aqt.dialogs.open("About", mw)``, and the DialogManager
    ``_dialogs`` registry stores a DIRECT reference to ``about.show`` captured
    at import time, so rebinding the module attribute alone would never
    intercept the menu path. Sanitizing the *data sources* sidesteps that
    registry-reference problem entirely: registry opens and direct
    ``aqt.about.show(mw)`` calls run the same module code.)

    Scoped to About only: ``aqt.errors`` (error reports) resolves ``supportText``
    through its OWN binding imported from ``aqt.utils`` and *defines*
    ``addon_debug_info`` itself, so real debug text still reaches error reports
    — verified against ``qt/aqt/errors.py``.

    Idempotent (re-runs skip already-sanitized symbols) and fail-open: a
    missing/renamed/reshaped symbol on any Anki version is skipped with a
    logged warning — About keeps working and the button then copies the real
    text rather than anything crashing. The stand-ins are constant-returning
    functions, so no exception can escape into Anki's click path.
    """
    import aqt.about

    for symbol, replacement_text in core.ABOUT_DEBUG_INFO_PATCHES.items():
        existing = getattr(aqt.about, symbol, None)
        if not callable(existing):
            print(
                f"[ci_buddy] warning: aqt.about.{symbol} not present or not "
                "callable on this Anki version (debug-info sanitizing skipped "
                "for it)"
            )
            continue
        if getattr(existing, _CI_BUDDY_SHIM_ATTR, False):
            continue  # already sanitized

        _original_about_debug_symbols[symbol] = existing

        def _sanitized(*_a: Any, _text: str = replacement_text, **_k: Any) -> str:
            # Constant text, whatever the caller passed — cannot raise.
            return _text

        setattr(_sanitized, _CI_BUDDY_SHIM_ATTR, True)
        setattr(aqt.about, symbol, _sanitized)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def register(config: dict[str, Any]) -> None:
    """Wire up all Part A hooks according to ``config``. Safe to call once at
    add-on init. Individual handlers are exception-wrapped.
    """
    from aqt import gui_hooks  # lazy — keeps this module import-safe for tests

    # Seam 4: install the write shim first (blocks durable config writes ASAP).
    if config.get("lock_addon_config_writes", True):
        _safe(install_write_config_shim)()

    # Seam 7: suppress automatic update checks. Applied NOW (at add-on load),
    # not on a hook: pm.meta is already loaded when add-ons import, and the
    # automatic checks fire inside loadProfile — which aqt runs BEFORE emitting
    # main_window_did_init (``on_window_init`` calls setupProfile first) — so a
    # hook-time force could lose that race on the very first launch check.
    if config.get("disable_update_checks", True):
        _safe(disable_automatic_update_checks)()

    # Seam 1: menu actions + whole menus, applied once the main window is built.
    gui_hooks.main_window_did_init.append(_safe(lambda: apply_menu_locks(config)))

    # Seam 2: programmatic-open guard, exact-name filtered and config-gated.
    locked_names = core.locked_dialog_names(config)
    if locked_names:
        gui_hooks.dialog_manager_did_open_dialog.append(
            _safe(
                lambda dm, name, inst: on_dialog_opened(
                    dm, name, inst, locked_names
                )
            )
        )

    # Seam 3: grey the Add-ons dialog (only meaningful if add-ons are locked).
    if config.get("lock_addons", True):
        gui_hooks.addons_dialog_will_show.append(
            _safe(lambda dialog: on_addons_dialog_will_show(dialog, config))
        )

    # Seam 6: disable the built-in Python debug console (arbitrary-code-exec
    # surface reachable over the noVNC desktop). Neuter the callable immediately,
    # then disable the already-built shortcut once the main window exists.
    if config.get("lock_debug_console", True):
        _safe(neuter_debug_console_callable)()
        gui_hooks.main_window_did_init.append(
            _safe(disable_debug_console_shortcut)
        )

    # Seam 8: sanitize About's "Copy Debug Info" (supportText + add-on list
    # leak environment data in the managed deployment; the dialog itself stays
    # visible — credits/AGPL fairness). Applied NOW (at add-on load), not on a
    # hook: aqt.about is already imported by aqt/__init__ before add-ons load,
    # there is no gui_hook for the About dialog, and the rebinding must be in
    # place before the first click.
    if config.get("sanitize_copy_debug_info", True):
        _safe(sanitize_about_debug_info)()

    # Seam 5: sync surfaces (config-gated; conservative defaults).
    if config.get("strip_sync_link", False):
        gui_hooks.top_toolbar_did_init_links.append(
            _safe(on_top_toolbar_did_init_links)
        )
    if config.get("disable_native_auto_sync", False):
        gui_hooks.profile_did_open.append(_safe(disable_native_auto_sync))
