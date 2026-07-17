# anki-ci-buddy

A minimal Anki add-on that turns a stock Anki install into a **locked appliance** for
hosted / CI "Anki-as-a-Service" deployments. One add-on, two config-gated modules:

- **`locks.py` — locks down the GUI.** A user can freely edit their collection (Add,
  Browse, Study, Decks, Stats, Note Types) and manage their own profiles (switch, add,
  rename, delete — since v0.7.0) but cannot tamper with the environment: no add-on
  install/remove, no add-on config edits, no Preferences, no app upgrade/downgrade, and
  the profile-manager screen's dangerous buttons (Open Backup / Quit / Downgrade) are
  locked.
- **`provisioners.py` — forces managed-environment config** the locked GUI can't
  reach: the collection's LaTeX-rendering flag, and hiding the sibling AnkiMCP
  add-on's UI surfaces.

ci-buddy **never touches AnkiWeb sync credentials**: users log into AnkiWeb manually
inside Anki over the VNC desktop, and — exactly like desktop Anki — that login persists
on the per-user persistent volume across pod restarts/sleep. It is wiped only when the
instance (and its PVC) is deleted. (The former credential-provisioning channel was
removed in v1.)

It is designed to be **baked into a [headless-anki](../headless-anki) image** (one more
directory under `/data/addons21/`) and to work over VNC, where the desktop GUI *is*
exposed to the customer. Sync *execution* is owned by the separate AnkiMCP server
add-on.

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
