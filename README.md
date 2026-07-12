# anki-ci-buddy

A minimal Anki add-on that turns a stock Anki install into a **locked appliance** for
hosted / CI "Anki-as-a-Service" deployments. One add-on, two config-gated modules:

- **`locks.py` — locks down the GUI.** A user can freely edit their collection (Add,
  Browse, Study, Decks, Stats, Note Types) but cannot tamper with the environment: no
  add-on install/remove, no add-on config edits, no Preferences, no profile switching,
  no app upgrade/downgrade.
- **`provisioning.py` — injects the AnkiWeb sync key (hkey).** Because locking
  Preferences removes the GUI's only sync-login surface, the add-on reads a delivered
  credentials file and applies it via `mw.pm.set_sync_key`, so sync keeps working. It
  picks up refreshed credentials in seconds and clears the key on teardown. The password
  never enters the pod — only a revocable token does.

It is designed to be **baked into a [headless-anki](../headless-anki) image** (one more
directory under `/data/addons21/`) and to work over VNC, where the desktop GUI *is*
exposed to the customer. Sync *execution* is owned by the separate AnkiMCP server
add-on; ci-buddy only seeds the credential.

## Why this exists

Cloud-hosted Anki instances need to behave like a locked appliance — similar to how a
Nix Anki install is "locked" — but the environment runs stock **PyPI `aqt`**, which
lacks the `ANKI_ADDONS` patch NixOS relies on. So the lock is implemented as a small
**software shim over public `aqt` GUI hooks**, not via a read-only filesystem (a
read-only add-ons dir only produces error dialogs and breaks add-ons' own runtime
writes — see REQUIREMENTS.md §7).

## Status

Specification only. See **[REQUIREMENTS.md](REQUIREMENTS.md)** for the full technical
spec to implement against.

## License

AGPL-3.0-or-later (matches the AnkiMCP add-on / Anki ecosystem).
