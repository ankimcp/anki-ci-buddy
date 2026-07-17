"""ci-buddy — Anki appliance lock + managed-environment config provisioners.

Entry point: read config, warn on unknown keys, install the writeConfig shim,
and register the locks and config-provisioner hooks. ci-buddy never touches
AnkiWeb sync credentials — users log into AnkiWeb manually over VNC and the
login persists on the per-user persistent volume. Everything is wrapped so no
failure here can crash Anki startup (fail open — spec §5).

Relative imports only (AnkiWeb installs use the numeric add-on id as the folder
name, which would break absolute imports).
"""

from __future__ import annotations

#: Guards against double hook registration if the module is imported/reloaded
#: more than once (e.g. a dev reload) — a second ``_load()`` becomes a no-op so
#: hooks aren't appended twice.
_registered = False


def _load() -> None:
    # Imports are inside the function so a partial failure is contained and a
    # bare `import ci_buddy` (e.g. during tooling) can't explode at module load.
    global _registered
    if _registered:
        return
    _registered = True

    from aqt import mw

    from . import compat, core, locks, provisioners

    # Read our own config early (the writeConfig shim only blocks *writes*, so
    # reads stay fine — but read before installing anything, to be safe).
    raw_config = None
    manager = getattr(mw, "addonManager", None)
    if manager is not None:
        try:
            raw_config = manager.getConfig(__name__)
        except Exception as exc:  # noqa: BLE001
            print(f"[ci_buddy] could not read config, using defaults: {exc!r}")

    config = core.merge_config(raw_config)
    core.warn_unknown_keys(config)

    # Seam 10: unsupported-Anki tripwire (a warning, never a gate) — first, so
    # the CI_BUDDY_UNSUPPORTED_ANKI stderr lines lead the pod log even if a
    # later registration fails.
    try:
        compat.register(config)
    except Exception as exc:  # noqa: BLE001 — never crash startup
        print(f"[ci_buddy] failed to register compat warning: {exc!r}")

    # GUI lock.
    try:
        locks.register(config)
    except Exception as exc:  # noqa: BLE001 — never crash startup
        print(f"[ci_buddy] failed to register locks: {exc!r}")

    # Config provisioners (RENDER_LATEX + AnkiMCP UI hide; each config-gated).
    try:
        provisioners.register(config)
    except Exception as exc:  # noqa: BLE001 — never crash startup
        print(f"[ci_buddy] failed to register provisioners: {exc!r}")


# Only run the aqt-dependent wiring when actually loaded inside Anki. Guarding on
# the import keeps `import ci_buddy.core` (unit tests) free of an aqt dependency.
try:
    import aqt  # noqa: F401
except ImportError:
    aqt = None  # type: ignore[assignment]

if aqt is not None:
    try:
        _load()
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        print(f"[ci_buddy] initialization failed: {exc!r}")
