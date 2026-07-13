"""Part B — sync-credential injection + collection-config provisioning.

Two independent, config-gated provisioners live here:

- ``SyncProvisioner`` reads a JSON credentials file at a config-driven path and
  injects the AnkiWeb sync key (hkey) + endpoint into the in-memory profile via
  ``mw.pm``, so sync works even though the GUI login surface (Preferences) is
  locked by Part A. The hkey is NEVER logged — only its fingerprint (spec §B.8).
- ``CollectionConfigProvisioner`` forces collection-level config that the locked
  Preferences GUI can no longer reach — currently only ``RENDER_LATEX`` (Anki's
  LaTeX image generation, OFF by default since 24.06 for security).

Both configure the user's data (profile / collection), so they share this
module. MUST NOT import ``locks`` — the two top-level modules are deliberately
decoupled.

``aqt`` (and ``anki``) are imported **lazily** inside the wiring methods so the
engines can be imported and unit-tested without a running Anki.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional, Protocol

from . import core


class _ProfileManager(Protocol):
    """The subset of ``aqt.profiles.ProfileManager`` this module touches.

    Declared as a Protocol so tests can pass a fake object (spec §8).
    """

    profile: Optional[dict]

    def set_sync_key(self, val: str | None) -> None: ...
    def set_sync_username(self, val: str | None) -> None: ...
    def set_custom_sync_url(self, url: str | None) -> None: ...
    def clear_sync_auth(self) -> None: ...
    def save(self) -> None: ...


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a hook/timer handler so any exception is logged, never propagated
    (fail open for the collection — spec §5)."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — fail open by design
            print(f"[ci_buddy] provisioning handler {fn.__name__} failed: {exc!r}")
        return None

    return wrapper


class SyncProvisioner:
    """Stateful credential injector.

    Holds the last-applied serial and file mtime so re-reads are cheap and
    re-injection happens only on a genuine serial increase.
    """

    def __init__(
        self,
        config: dict[str, Any],
        printer: Callable[[str], None] = print,
    ) -> None:
        self._config = config
        self._print = printer
        self.last_applied_serial: int | None = None
        self._last_stat: tuple[float, int] | None = None
        self._warned_missing = False
        self._timer: Any = None  # QTimer, created lazily in _start_timer()

    # -- config accessors --------------------------------------------------- #

    @property
    def credentials_path(self) -> str:
        return str(
            self._config.get(
                "credentials_path", core.DEFAULT_CONFIG["credentials_path"]
            )
        )

    @property
    def _allowlist(self) -> list[str]:
        allow = self._config.get(
            "endpoint_allowlist", core.DEFAULT_CONFIG["endpoint_allowlist"]
        )
        return list(allow) if allow else []

    @property
    def _poll_ms(self) -> int:
        seconds = self._config.get(
            "credentials_poll_seconds",
            core.DEFAULT_CONFIG["credentials_poll_seconds"],
        )
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = core.DEFAULT_CONFIG["credentials_poll_seconds"]
        # Clamp to a sane floor so a bad config can't busy-spin the timer.
        return max(500, int(seconds * 1000))

    # -- file reading ------------------------------------------------------- #

    def _read_raw(self) -> str | None:
        """Read the credentials file. Return its text, or ``None`` if absent or
        unreadable. Absent → one warning (then quiet); other IO errors are
        silent no-ops retried next tick (spec §B.2/B.3)."""
        path = self.credentials_path
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
            self._warned_missing = False
            return raw
        except FileNotFoundError:
            if not self._warned_missing:
                self._print(
                    f"[ci_buddy] credentials file not found at {path!r} "
                    "(provisioning idle until it appears)"
                )
                self._warned_missing = True
            return None
        except OSError as exc:
            # Permissions / partial mount / transient IO — retry next tick.
            self._print(f"[ci_buddy] could not read credentials file: {exc!r}")
            return None

    # -- injection ---------------------------------------------------------- #

    def apply_to_pm(self, pm: _ProfileManager, creds: core.Credentials) -> None:
        """Apply a validated credentials record to the profile manager.

        Sequence (spec §B.3): set key → (allowlisted) endpoint → username →
        ``save()``. Never logs the hkey.
        """
        pm.set_sync_key(creds.hkey)

        if creds.endpoint:
            if core.endpoint_allowed(creds.endpoint, self._allowlist):
                pm.set_custom_sync_url(creds.endpoint)
            else:
                # Rogue/unlisted endpoint → keep default AnkiWeb (spec §B.4).
                self._print(
                    f"[ci_buddy] endpoint {creds.endpoint!r} not in allowlist "
                    "— ignoring, using default sync server"
                )
        else:
            # No endpoint → revert to default AnkiWeb, clearing any custom URL a
            # previous (higher-privileged) serial may have set. Passing None to
            # set_custom_sync_url also resets currentSyncUrl (aqt/profiles.py),
            # so no stale endpoint residue survives in prefs21.db (spec §B.3).
            pm.set_custom_sync_url(None)

        if creds.username:
            pm.set_sync_username(creds.username)

        pm.save()

        self._print(
            f"[ci_buddy] applied sync credentials "
            f"(serial={creds.serial}, hkey={core.hkey_fingerprint(creds.hkey)})"
        )

    def maybe_inject(self, pm: _ProfileManager, *, force: bool = False) -> bool:
        """Read the file and inject if the serial increased. Returns True iff a
        new credential was applied. Never raises on bad input.

        ``force`` bypasses the mtime fast-path (used on ``profile_did_open``).
        """
        path = self.credentials_path
        if not force:
            # Fast-path: skip read+parse if unchanged. Key on (mtime, size) so an
            # atomic same-mtime rewrite of a different length is still noticed.
            try:
                st = os.stat(path)
                stamp: tuple[float, int] | None = (st.st_mtime, st.st_size)
            except OSError:
                stamp = None
            if stamp is not None and stamp == self._last_stat:
                return False
            self._last_stat = stamp

        raw = self._read_raw()
        if raw is None:
            return False

        try:
            creds = core.parse_credentials(raw)
        except core.CredentialsError as exc:
            self._print(f"[ci_buddy] ignoring malformed credentials file: {exc}")
            return False

        if not core.should_inject(creds.serial, self.last_applied_serial):
            return False

        self.apply_to_pm(pm, creds)
        self.last_applied_serial = creds.serial
        return True

    def clear_key(self, pm: _ProfileManager) -> None:
        """Clear the sync key/username from the profile and persist (spec §B.5).

        Keeps the at-rest ``prefs21.db`` free of the credential-bearing hkey;
        re-injection on the next ``profile_did_open`` makes this nearly free.

        ``clear_sync_auth`` (aqt/profiles.py) clears syncKey + syncUser + hostNum
        + currentSyncUrl, but NOT customSyncUrl — so we also reset the custom URL
        so no endpoint residue survives in the at-rest profile.
        """
        pm.clear_sync_auth()
        pm.set_custom_sync_url(None)
        pm.save()
        # Force a fresh inject next time the file is (re)read.
        self.last_applied_serial = None
        self._last_stat = None
        self._print("[ci_buddy] cleared sync key on profile close")

    # -- aqt wiring (lazy imports; not exercised by unit tests) ------------- #

    def _current_pm(self) -> _ProfileManager | None:
        """Return ``mw.pm`` iff a profile is open, else ``None``.

        Guards against the timer firing during profile open/close transitions
        (spec §B.3).
        """
        from aqt import mw  # lazy — keeps this module import-safe for tests

        pm = getattr(mw, "pm", None)
        if pm is None or getattr(pm, "profile", None) is None:
            return None
        return pm

    def on_profile_did_open(self) -> None:
        """Re-inject on every profile open (not just the first)."""
        pm = self._current_pm()
        if pm is not None:
            self.maybe_inject(pm, force=True)
        self._start_timer()

    def on_profile_will_close(self) -> None:
        if not self._config.get("clear_key_on_close", True):
            return
        pm = self._current_pm()
        if pm is not None:
            self.clear_key(pm)

    def _poll(self) -> None:
        pm = self._current_pm()
        if pm is not None:
            self.maybe_inject(pm, force=False)

    def _start_timer(self) -> None:
        """Create/start the main-thread mtime-poll QTimer (idempotent)."""
        if self._timer is not None:
            return
        from aqt import mw  # lazy
        from aqt.qt import QTimer  # lazy

        timer = QTimer(mw)
        timer.setInterval(self._poll_ms)
        timer.timeout.connect(_safe(self._poll))
        timer.start()
        self._timer = timer


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
        self._print("[ci_buddy] enabled LaTeX image generation (RENDER_LATEX)")
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


def register(config: dict[str, Any]) -> SyncProvisioner:
    """Wire up Part B hooks. Returns the ``SyncProvisioner`` (mainly for testing
    / introspection).

    The two provisioners are independently config-gated: credential injection on
    ``provisioning_enabled`` and LaTeX enforcement on ``ensure_latex_generation``.
    """
    provisioner = SyncProvisioner(config)
    latex_provisioner = CollectionConfigProvisioner(config)

    provisioning_on = config.get("provisioning_enabled", False)
    latex_on = config.get("ensure_latex_generation", True)

    if not provisioning_on and not latex_on:
        return provisioner

    from aqt import gui_hooks  # lazy

    if provisioning_on:
        gui_hooks.profile_did_open.append(_safe(provisioner.on_profile_did_open))
        gui_hooks.profile_will_close.append(
            _safe(provisioner.on_profile_will_close)
        )

    if latex_on:
        gui_hooks.collection_did_load.append(
            _safe(latex_provisioner.on_collection_did_load)
        )
        gui_hooks.sync_did_finish.append(
            _safe(latex_provisioner.on_sync_did_finish)
        )

    return provisioner
