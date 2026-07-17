"""Config provisioners for the managed environment.

Two independent, config-gated provisioners live here:

- ``CollectionConfigProvisioner`` forces collection-level config that the locked
  Preferences GUI can no longer reach — currently only ``RENDER_LATEX`` (Anki's
  LaTeX image generation, OFF by default since 24.06 for security).
- ``AddonConfigProvisioner`` coerces a *sibling* add-on's config — currently
  hiding the AnkiMCP server add-on's user-facing UI surfaces.

ci-buddy never touches AnkiWeb sync credentials: users log into AnkiWeb
manually inside Anki (over the VNC desktop) and that login persists on the
per-user persistent volume like on any desktop install.

Both provisioners configure the user's data (collection / sibling meta.json),
so they share this module. MUST NOT import ``locks`` — the two top-level
modules are deliberately decoupled.

``aqt`` (and ``anki``) are imported **lazily** inside the wiring methods so the
engines can be imported and unit-tested without a running Anki.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from . import core


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a hook/timer handler so any exception is logged, never propagated
    (fail open for the collection — spec §5)."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail open by design
            print(f"[ci_buddy] provisioner handler {fn.__name__} failed: {exc!r}")
        return None

    return wrapper


class CollectionConfigProvisioner:
    """Force collection-level config that the locked Preferences GUI can't reach.

    Currently this only turns Anki's LaTeX image generation ON: the collection
    boolean ``Config.Bool.RENDER_LATEX`` is OFF by default since Anki 24.06 (a
    security default — arbitrary shell-out to the LaTeX toolchain), so cards with
    ``[latex]`` / TikZ show "LaTeX image generation is disabled in the
    preferences" and never compile. On a hosted pod Preferences is locked by
    Part A, so the flag must be forced in code.

    Applied on collection load AND after every sync: the hosted pod syncs the
    user's collection from AnkiWeb on startup, and the synced-down value may be
    OFF, which would override an early set. Anki reads the flag fresh at render
    time, so it just needs to be true before any card renders.

    ``ensure_render_latex`` reads the current value first and writes only when it
    is off — idempotent, and it avoids re-dirtying the collection after each sync
    (which would otherwise cause a set-modify-upload loop on every sync).
    """

    def __init__(
        self,
        config: dict[str, Any],
        printer: Callable[[str], None] = print,
    ) -> None:
        self._config = config
        self._print = printer

    def ensure_render_latex(self, col: Any) -> bool:
        """Set ``RENDER_LATEX`` true on ``col`` if it is currently off.

        Returns True iff it changed the value (was off → now on); False if the
        collection is missing or the flag was already on (idempotent no-op).
        """
        if col is None:
            return False

        from anki.config import Config  # lazy — no anki import at module load

        if col.get_config_bool(Config.Bool.RENDER_LATEX):
            return False  # already on — nothing to do (and don't re-dirty)

        col.set_config_bool(Config.Bool.RENDER_LATEX, True)
        self._print(
            "[ci_buddy] force-enabled RENDER_LATEX (LaTeX image generation) "
            "on the collection"
        )
        return True

    def _current_col(self) -> Any:
        """Return ``mw.col`` if a collection is open, else ``None``."""
        from aqt import mw  # lazy — keeps this module import-safe for tests

        return getattr(mw, "col", None)

    def on_collection_did_load(self, col: Any) -> None:
        """Force RENDER_LATEX as soon as the collection is loaded."""
        self.ensure_render_latex(col)

    def on_sync_did_finish(self) -> None:
        """Re-force RENDER_LATEX after a sync, in case AnkiWeb sent down OFF."""
        self.ensure_render_latex(self._current_col())


def _real_write_config(manager: Any) -> Callable[..., Any] | None:
    """Return the ``addonManager.writeConfig`` that actually persists to
    ``meta.json`` — even after locks Seam 4 has shimmed it to drop writes.

    ``locks.install_write_config_shim`` replaces ``manager.writeConfig`` with a
    shim that silently DROPS every write, and stashes the genuine original on
    that shim under ``core.ORIGINAL_WRITE_CONFIG_ATTR`` (the shared constant —
    single source of truth for both sides of the contract). We recover it here
    so ci-buddy can durably write a *sibling* add-on's config (AnkiMCP's toolbar
    indicator).

    This is deliberately robust to registration order and does NOT import
    ``locks`` (the two modules stay decoupled): if the shim is installed — no
    matter whether ``locks.register`` ran before or after us — the original is on
    the attribute; if no shim is installed (``lock_addon_config_writes: false``,
    or the provisioners wired before locks), the live ``writeConfig`` is already
    the real one and ``getattr`` falls through to it.
    """
    write_config = getattr(manager, "writeConfig", None)
    if write_config is None:
        return None
    return getattr(write_config, core.ORIGINAL_WRITE_CONFIG_ATTR, write_config)


def _installed_addon_packages(manager: Any) -> list[tuple[str, str | None]]:
    """Return ``(dir_name, manifest_package)`` for every installed add-on.

    Enumerates ``manager.allAddons()`` — aqt's long-stable lister that returns
    installed add-on *directory* names — and reads each add-on's on-disk
    ``manifest.json`` for its declared ``package``. That manifest field is the
    only place the package survives: Anki's ``meta.json`` (``addonMeta``) stores
    the human ``name`` (``provided_name``) but NOT ``package``, so it cannot be
    used to recover it.

    Defensive by design: an add-on whose manifest is absent, unreadable, not an
    object, or lacks a string ``package`` contributes ``None`` (it simply never
    matches). Never raises — the caller (``hide_ankimcp_ui``) is itself
    ``_safe``-wrapped, but this keeps a single malformed sibling from hiding the
    others.
    """
    all_addons = getattr(manager, "allAddons", None)
    addons_folder = getattr(manager, "addonsFolder", None)
    if all_addons is None or addons_folder is None:
        return []
    pairs: list[tuple[str, str | None]] = []
    for dir_name in all_addons():
        package: str | None = None
        try:
            manifest_path = os.path.join(addons_folder(dir_name), "manifest.json")
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            pkg = manifest.get("package")
            if isinstance(pkg, str):
                package = pkg
        except (OSError, ValueError, AttributeError):
            package = None
        pairs.append((dir_name, package))
    return pairs


class AddonConfigProvisioner:
    """Coerce a *sibling* add-on's config in the managed environment.

    Currently this hides AnkiMCP's user-facing UI surfaces: the AnkiMCP server
    add-on (manifest ``package = anki_mcp_server``) ships, defaulted true, both a
    persistent "[• AnkiMCP]" toolbar button (``show_toolbar_indicator``) and an
    "AnkiMCP Server Settings..." entry in Anki's Tools menu
    (``show_settings_menu_item``). On the hosted appliance those surfaces are
    unwanted, so ci-buddy forces the corresponding keys false — each gated
    independently by its own ci-buddy ``hide_ankimcp_*`` flag
    (``core.ANKIMCP_HIDE_KEY_MAP``). Both keys are written in ONE getConfig /
    writeConfig round-trip.

    The sibling is addressed by resolving its install *directory* from the
    manifest ``package`` (``_installed_addon_packages`` +
    ``core.resolve_addon_dir_by_package``) — NOT by assuming the directory name
    equals the package. On the hosted image the directory is ``ankimcp`` while
    the manifest package stays ``anki_mcp_server``; the old assumption made
    ``getConfig``/``writeConfig`` (which key on the directory) silently no-op.

    Unlike ``CollectionConfigProvisioner`` this runs at ADD-ON LOAD time, NOT on
    a gui_hook. AnkiMCP reads both keys exactly once, inside its
    ``main_window_did_init`` handler (``_setup_menu``), and aqt fires that hook
    only after every add-on has finished importing. ci-buddy writes the values
    during its own import — strictly before that hook — so the surfaces are
    hidden the SAME session, not merely next launch. (Registering the write on
    ci-buddy's own ``main_window_did_init`` would be too late: AnkiMCP loads
    first alphabetically, so its handler runs before ours.)

    The write goes through ``_real_write_config`` so it survives Seam 4's
    write-dropping shim; if AnkiMCP isn't installed or every gated key is already
    false, it is a safe no-op (see ``core.plan_hide_ankimcp_ui``).
    """

    def __init__(
        self,
        config: dict[str, Any],
        printer: Callable[[str], None] = print,
    ) -> None:
        self._config = config
        self._print = printer

    def hide_ankimcp_ui(self, manager: Any) -> bool:
        """Force the ci-buddy-gated AnkiMCP UI keys false via ``manager`` (an
        ``addonManager``), in a single getConfig/writeConfig pass. Returns True
        iff it wrote a change.

        Resolves AnkiMCP's install *directory* by matching its manifest
        ``package`` (``core.ANKIMCP_PACKAGE``) across installed add-ons, then
        keys ``getConfig``/``writeConfig`` on that directory — so it works on the
        hosted image where the directory (``ankimcp``) differs from the package.

        No-op (False) when: no manager, no add-on declares the package (the
        resolver logs a distinctive warning), AnkiMCP has no config, or every
        gated key is already false. Fail-open — the caller wraps this in
        ``_safe`` so a broken/absent AnkiMCP can never crash startup.
        """
        if manager is None:
            return False
        get_config = getattr(manager, "getConfig", None)
        if get_config is None:
            return False

        module = core.resolve_addon_dir_by_package(
            _installed_addon_packages(manager), core.ANKIMCP_PACKAGE, self._print
        )
        if module is None:
            return False  # AnkiMCP not found — resolver already warned (fail-open)

        current = get_config(module)
        new_config = core.plan_hide_ankimcp_ui(self._config, current)
        if new_config is None:
            return False  # no config / every gated key already false

        write_config = _real_write_config(manager)
        if write_config is None:
            return False
        write_config(module, new_config)
        # The plan only touches the gated AnkiMCP keys, so a diff against the
        # pre-write config is exactly the set it forced — report it for the log.
        forced = sorted(k for k, v in new_config.items() if current.get(k) is not v)
        self._print(
            "[ci_buddy] hid AnkiMCP UI "
            f"(forced dir {module!r} [package {core.ANKIMCP_PACKAGE}] "
            f"{', '.join(f'{k}=false' for k in forced)})"
        )
        return True

    def _current_manager(self) -> Any:
        """Return ``mw.addonManager`` if present, else ``None``."""
        from aqt import mw  # lazy — keeps this module import-safe for tests

        return getattr(mw, "addonManager", None)

    def on_register(self) -> None:
        """Apply the coercion once, at add-on load time."""
        self.hide_ankimcp_ui(self._current_manager())


def register(config: dict[str, Any]) -> None:
    """Wire up the config provisioners.

    They are independently config-gated: LaTeX enforcement on
    ``ensure_latex_generation``, and the AnkiMCP UI hide on the
    ``hide_ankimcp_*`` gates (``core.ANKIMCP_HIDE_KEY_MAP``) — the coercion runs
    if *any* gate is on, and forces only the enabled keys in one write.
    """
    latex_on = config.get("ensure_latex_generation", True)
    hide_ankimcp_ui_on = any(
        config.get(gate, False) for gate in core.ANKIMCP_HIDE_KEY_MAP
    )

    # Sibling-add-on coercion runs NOW (at load), not on a hook — it must land
    # before AnkiMCP reads the flags at main_window_did_init. See
    # AddonConfigProvisioner for the same-session-vs-next-launch reasoning.
    if hide_ankimcp_ui_on:
        _safe(AddonConfigProvisioner(config).on_register)()

    if not latex_on:
        return

    from aqt import gui_hooks  # lazy

    latex_provisioner = CollectionConfigProvisioner(config)
    gui_hooks.collection_did_load.append(
        _safe(latex_provisioner.on_collection_did_load)
    )
    gui_hooks.sync_did_finish.append(
        _safe(latex_provisioner.on_sync_did_finish)
    )
