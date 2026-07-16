# ci-buddy — Anki add-on

A small, dependency-free Anki add-on with **two independent, config-gated modules**:

- **GUI lock** (`locks.py`) — turns Anki into a locked appliance: no
  Preferences, no Add-ons install/remove/config, no profile switch, no app
  upgrade. Collection editing (Add / Browse / Study / Decks / Stats / **Manage
  Note Types**) stays fully usable.
- **Sync-credential provisioning** (`provisioning.py`) — reads a JSON
  credentials file and injects the AnkiWeb sync key (hkey) + endpoint into the
  profile via `mw.pm`, so sync works even though the GUI login surface
  (Preferences) is locked.

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
├── locks.py          # Part A — GUI lock (does NOT import provisioning)
├── provisioning.py   # Part B — credential injection (does NOT import locks)
├── core.py           # pure logic, no aqt (config, serial, allowlist, fingerprint)
├── config.json       # defaults
├── config.md         # per-option documentation
└── manifest.json
```

## Configuration

Every lock and provisioning behaviour is individually toggleable. See
[`ci_buddy/config.md`](ci_buddy/config.md) for the full option reference and the
credentials-file contract.

Notable defaults (conservative — a non-hosted user is never surprised):

- `provisioning_enabled: false` — the **hosted image sets this to `true`**.
- `strip_sync_link` / `disable_native_auto_sync: false` — these stay `false`
  even in the hosted image (native auto-sync stays ON; see config.md).

## Build

```bash
./package.sh          # → ci_buddy.ankiaddon (manifest.json at zip root)
```

The `.ankiaddon` (or the raw `ci_buddy/` directory dropped into
`/data/addons21/ci_buddy/`) is the deliverable for the headless-anki image.

## Test

Pure-logic + provisioning tests run without a running Anki (stdlib + pytest):

```bash
python3 -m pytest tests -q
```

GUI locking is hard to unit-test headlessly; those assertions are covered by an
E2E test over VNC in headless-anki (see REQUIREMENTS §8) — the acceptance
checklist there is the manual fallback.
