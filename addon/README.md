# ci-buddy — Anki add-on

A small, dependency-free Anki add-on with **two independent, config-gated modules**:

- **GUI lock** (`locks.py`) — turns Anki into a locked appliance: no
  Preferences, no Add-ons install/remove/config, no app upgrade. Collection
  editing (Add / Browse / Study / Decks / Stats / **Manage Note Types**) stays
  fully usable, and users manage their own profiles (switch / add / rename /
  delete — v0.7.0) with the profile-manager's dangerous buttons locked.
- **Config provisioners** (`provisioners.py`) — force managed-environment
  config the locked GUI can't reach: the collection's `RENDER_LATEX` flag, and
  hiding the sibling AnkiMCP add-on's UI surfaces.

ci-buddy **never touches AnkiWeb sync credentials**: the user logs into AnkiWeb
manually inside Anki over the VNC desktop, and that login persists in
`prefs21.db` on the per-user persistent volume — exactly like desktop Anki — and
is wiped only when the instance (and its PVC) is deleted. (The former
credential-provisioning channel was removed in v1.)

The two modules never import each other. All logic decisions live in `core.py`
(no `aqt` import → unit-testable). Every hook handler is exception-wrapped so
nothing here can crash Anki startup or block the collection.

Full spec: [`../REQUIREMENTS.md`](../REQUIREMENTS.md). Platform context:
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

Target: PyPI `aqt` 26.5+, Python 3.12+.

## Layout

```
ci_buddy/
├── __init__.py       # entry: read config, install writeConfig shim, register hooks
├── locks.py          # GUI lock (does NOT import provisioners)
├── provisioners.py   # collection + sibling add-on config coercion (does NOT import locks)
├── compat.py         # Seam 10: unsupported-Anki warning (a warning, never a gate)
├── core.py           # pure logic, no aqt (config surface, lock plans, version matrix)
├── config.json       # defaults
├── config.md         # per-option documentation
└── manifest.json
```

## Configuration

Every behaviour is individually toggleable. See
[`ci_buddy/config.md`](ci_buddy/config.md) for the full option reference.

Notable defaults (conservative — a non-hosted user is never surprised):

- `strip_sync_link` / `disable_native_auto_sync: false` — these stay `false`
  even in the hosted image (native auto-sync stays ON; see config.md).
- `warn_unsupported_anki: true` — on an Anki version not in the hardcoded
  verified matrix (`SUPPORTED_ANKI_VERSIONS` in `core.py`), print a loud
  greppable stderr warning (`CI_BUDDY_UNSUPPORTED_ANKI`) and show a red banner
  in the GUI. A **warning, not a gate**: Anki still starts and every seam still
  applies (fail-open as usual). See config.md, Seam 10.

## Build

```bash
./package.sh          # → ci_buddy.ankiaddon (manifest.json at zip root)
```

The `.ankiaddon` (or the raw `ci_buddy/` directory dropped into
`/data/addons21/ci_buddy/`) is the deliverable for the headless-anki image.

## Test

Pure-logic + provisioner tests run without a running Anki (stdlib + pytest):

```bash
python3 -m pytest tests -q
```

GUI locking is hard to unit-test headlessly; those assertions are covered by an
E2E test over VNC in headless-anki (see REQUIREMENTS §8) — the acceptance
checklist there is the manual fallback.
