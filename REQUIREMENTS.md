# anki-ci-buddy — Technical Requirements

Implementation spec for a small Anki add-on that (A) **locks the GUI** of a hosted / CI
Anki instance and (B) **forces managed-environment config** the locked GUI can no longer
reach (collection LaTeX rendering, sibling add-on UI). Written for an implementing agent.
Target: **PyPI `aqt` 26.5+, Python 3.12+** (the versions
[headless-anki](../headless-anki) ships). Line numbers below are guidance,
not contracts — resolve symbols by name.

> **Sync credentials: ci-buddy never touches them.** The former Part B
> (credential provisioning) was **removed in v1** — see [Part B](#part-b--sync-credentials-removed-in-v1)
> for the replacement posture (manual AnkiWeb login over VNC, persisted on the PVC).

The add-on is **service-context software** (not the general-purpose AnkiMCP add-on), so
service-branded messaging like *"Managed environment — locked"* is acceptable here. Keep
brand strings in one constant.

---

## 1. Goal & scope

`anki-ci-buddy` is **one add-on with two independent, config-gated modules**:

- **`locks.py`** — make Anki a locked appliance (no add-on install/remove/config, no
  Preferences, no app upgrade), while collection editing, Note Types, and self-service
  profile management (switch / add / rename / delete — v0.7.0, Seam 9 locks the
  profile-manager's dangerous buttons) stay fully usable.
- **`provisioners.py`** — force managed-environment config the locked GUI can't reach:
  the collection's `RENDER_LATEX` flag, and the sibling AnkiMCP add-on's UI-surface
  config keys. `provisioners.py` MUST NOT import `locks.py` (keep the two concerns
  decoupled).

**In scope:** the GUI lock; the collection/sibling config provisioners.

**Out of scope (other repos — do NOT build here):**
- **Sync execution.** The **AnkiMCP server add-on** owns all sync via an async job model
  (`sync()` → `job_id` → poll → `resolve`).
- **Sync credentials, in any form.** Removed in v1 (see Part B): the user logs into
  AnkiWeb manually over VNC; no component mints, delivers, or injects an hkey.
- Provisioning dashboards, persistent-volume durability, VNC plumbing.

---

# Part A — GUI Lock

## A.2. Mechanism — software shim, NOT filesystem perms

Implement the lock with **public `aqt` GUI hooks + a `writeConfig` monkeypatch** — the
same technique NixOS's injected lock add-on uses. It is the only correct approach here:

1. PyPI `aqt` has **no `ANKI_ADDONS` env-var patch** (nixpkgs-only), so the Nix injection
   path is unavailable.
2. A read-only add-ons dir does **not** disable any control — buttons stay clickable and
   throw `OSError` → warning dialog. It also breaks add-ons' own runtime writes (§C.1).
   Genuinely *disabled* controls require the hook approach.

Hooks used exist and are stable across 25.x (no renames observed): `main_window_did_init`,
`dialog_manager_did_open_dialog`, `addons_dialog_will_show`, `top_toolbar_did_init_links`.

## A.3. What to lock — ranked seams (implement in this order)

Prefer disabling the **menu action** over disabling a shown dialog: nothing gets
constructed, no visible flash, and `mw.form` action names (`_aqt/forms/main_qt6.py`) are
stable across versions.

### Seam 1 (primary): disable menu actions on `main_window_did_init`

| Action attr on `mw.form`      | Menu item                | Lock? |
|-------------------------------|--------------------------|-------|
| `actionPreferences`           | Tools → Preferences      | YES   |
| `actionAdd_ons`               | Tools → Add-ons          | YES   |
| `actionSwitchProfile`         | File → Switch Profile    | **NO — keep enabled** (multi-profile support, v0.7.0; the profile-manager screen it opens is locked down per-button instead) |
| `action_upgrade_downgrade`    | Tools → Upgrade/Downgrade | YES (assert; usually auto-hidden when no launcher) |
| `actionNoteTypes`             | Tools → Manage Note Types | **NO — keep enabled** |

Default to `setVisible(False)` (item disappears); `setEnabled(False)` (greyed-visible) is
a config choice (§4, `hide_vs_disable`). Re-apply on `profile_did_open` **only if** you
empirically observe another add-on re-enabling them — don't add it speculatively.

### Seam 2 (belt-and-suspenders): `dialog_manager_did_open_dialog`, filtered
Catches *programmatic* opens that bypass the menu. `setEnabled(False)` **only** for:

```
name in {"Preferences", "AddonsDialog"}
```

> ⚠️ **CRITICAL — never run this handler unfiltered.** The same DialogManager registry
> also serves `AddCards`, `Browser`, `EditCurrent`, `FilteredDeckConfigDialog`,
> `DeckStats`. An unfiltered `setEnabled(False)` would break the collection-editing UI we
> must keep working. Match exact names only. (Fires *after* `show()` → brief flash;
> acceptable as a secondary guard — Seam 1 prevents the normal path.)

### Seam 3: `addons_dialog_will_show` → `dialog.setEnabled(False)`
Greys the entire Add-ons dialog before it shows. Optionally set the window title to the
`managed_by_label`. This is the exact seam NixOS uses.

### Seam 4: neuter add-on config persistence — override `writeConfig`
Replace `mw.addonManager.writeConfig` with a **wrap-and-delegate** shim that does **not**
call through (silently drops writes; optionally logs). Save the original reference.
`mw.addonManager` exists early, so install at add-on import or in the init hook.

> Config + enabled/disabled state persist to `<addons21>/<module>/meta.json` via
> `writeConfig → writeAddonMeta`. Blocking it makes config changes non-durable without
> touching the filesystem.
>
> **Do not neuter ci-buddy's / other add-ons' ability to *read* their own config**, and
> ensure ci-buddy reads its own config *before* the shim would interfere (it only
> blocks writes, so reads are fine — but read early to be safe).

### Seam 5 (config-gated): sync surfaces
Sync is **not** a menu item. For a fully deterministic, operator-driven-only sync setup,
the add-on can strip the top-toolbar sync link via `top_toolbar_did_init_links` (filter
the link whose id is `sync`) and disable native auto-sync (in-memory `autoSync` flag).

> **Hosted defaults (platform decision 14 — supersedes the earlier "hosted → true"
> guidance):** the hosted pod behaves like a regular Anki desktop device, with the sync
> link visible and native auto-sync **ON** — so `strip_sync_link` /
> `disable_native_auto_sync` stay `false` in the hosted config too. The toolbar sync
> link is also the user's AnkiWeb **login surface** (Part B): clicking Sync while logged
> out opens Anki's stock sync-login prompt over VNC.

### Borderline actions — config-gated (default = locked)
Destructive (can replace/destroy the whole collection): `actionImport` (File → Import),
`action_open_backup` (File → Open Backup) → default **LOCK**. Low-risk maintenance:
`actionFullDatabaseCheck` (Check Database), Check Media → default **UNLOCKED**, toggle
exposed.

## A.4. Must stay enabled (never disable)
Add, Browse, Study/Review, Decks, Stats, per-deck options, Undo/Redo, and **Manage Note
Types** (explicit product decision — users edit templates/CSS). If unsure whether a
dialog name is safe to touch, do nothing rather than risk breaking Add/Browse.

---

# Part B — Sync credentials: REMOVED in v1

**Platform decision (2026-07): the hosted platform's GUI credential channel is removed
from v1.** Earlier revisions of this spec defined a full credential-provisioning channel
here (§B.1–§B.8: hkey minting control-plane-side, a mounted
`/run/ankimcp/sync-credentials.json` file contract with monotonic `serial`, injection via
`mw.pm.set_sync_key`, an endpoint allowlist, clear-key-on-close, fingerprint-only
logging). **All of it is deleted** — the lifecycle service no longer performs the
password→hkey exchange, no per-user sync-credentials k8s Secret is written or mounted,
and ci-buddy never touches sync credentials. The old §B.x numbers are retained here only
so cross-references in other docs resolve to this notice.

**The replacement posture:**

- The user logs into AnkiWeb **manually inside Anki, over the VNC remote desktop** —
  Anki's stock sync-login prompt (e.g. from the toolbar Sync button) is the login
  surface. Preferences stays locked (Part A).
- The login persists in `prefs21.db` on the **per-user persistent volume** across pod
  restarts and scale-to-zero sleep — **exactly like desktop Anki**. There is deliberately
  **no clear-on-close**: a retained clear-key hook would silently log the user out on
  every profile close.
- The login is wiped only when the **instance (and its PVC) is deleted**.
- `prefs21.db` at rest on the PVC therefore carries the user's AnkiWeb sync key, exactly
  as on any desktop install. Treat the volume as secret-grade — it already holds the full
  collection, so this adds no new custody class.
- **ci-buddy must not read, write, log, or clear any sync credential.** No password, no
  hkey, and no k8s credential ever enters the add-on.
- Cross-repo note (kept from old §B.7): the AnkiMCP sync job builds `SyncAuth` fresh per
  run from `mw.pm.sync_auth()`, so a re-login over VNC is picked up by the next sync
  with no restart.

---

## 4. Config surface (`config.json`)

Every behavior is individually toggleable. Proposed **add-on defaults** (conservative —
they are also the hosted defaults; the hosted image no longer flips any sync option,
platform decision 14):

```json
{
  "managed_by_label": "Managed environment — settings are locked",
  "hide_vs_disable": "hide",

  "lock_preferences": true,
  "lock_addons": true,
  "lock_addon_config_writes": true,
  "lock_profile_switch": false,
  "lock_upgrade_downgrade": true,
  "lock_note_types": false,
  "lock_import": true,
  "lock_export": true,
  "lock_create_backup": true,
  "lock_open_backup": true,
  "lock_exit": true,
  "lock_database_check": false,
  "lock_file_menu": false,
  "lock_profile_manager_open_backup": true,
  "lock_profile_manager_quit": true,
  "lock_profile_manager_downgrade": true,
  "strip_sync_link": false,
  "disable_native_auto_sync": false
}
```

- `hide_vs_disable`: `"hide"` → `setVisible(False)`; `"disable"` → `setEnabled(False)`.
- Unknown/typo keys → `print()` a warning (visible in container logs), never crash.

## 5. Non-goals / guardrails
- Do **not** disable any collection-editing surface (§A.4). When in doubt, do nothing.
- Do **not** implement filesystem read-only locking here (§A.2, §C.1).
- Do **not** touch sync credentials (Part B), hardcode a dashboard URL, drive sync, or
  reach the k8s API. ci-buddy calls Anki APIs — nothing more.
- **Fail open for the collection, closed for settings:** any exception in a lock or
  provisioner handler must be caught and logged, never crash startup or block the
  collection.

## C. Interaction with headless-anki / the operator (deployment constraints)
The implementing agent should surface these to the operator / headless-anki repos; they
are *their* responsibility but ci-buddy's correctness depends on them:

1. **Why the shim, not FS perms (§A.2):** Anki writes inside the add-on dir at runtime —
   config → `<addons21>/<module>/meta.json`; add-ons keep `user_files/` *inside* their own
   dir (the AnkiMCP add-on writes `user_files/credentials.json`, `user_files/ankimcp.log`).
   A blanket read-only `addons21` mount breaks these and first-run `meta.json` creation.
2. **Credential delivery: REMOVED in v1** (see Part B). There is no sync-credentials
   Secret, no mount, and no Secret-watching sidecar — AnkiWeb login happens manually over
   VNC and persists on the PVC. ci-buddy holds **no** k8s creds.
3. **Durability — `preStop` sync is NOT a safety net.** A `preStop` MCP sync is bounded by
   `terminationGracePeriodSeconds` (media-heavy syncs can be SIGKILLed mid-flight) and
   never runs on OOM/eviction/node-loss. An ungraceful pod death loses everything since
   the last sync. The **real** safety net is `/data` on a **per-user persistent volume**
   (and/or periodic syncs); `preStop` sync is best-effort polish on top.
4. Profile-level state (`prefs21.db`) lives outside the add-ons dir, on the PVC. After the
   user's manual AnkiWeb login it carries their sync key at rest (Part B) — treat that
   volume as secret-grade (it already holds the full collection).

## 6. Add-on structure
Tiny, dependency-free (stdlib + `aqt`/`anki` only — no vendoring, no `user_files`).

```
ci_buddy/
├── __init__.py        # entry: register hooks, install writeConfig shim
├── locks.py           # GUI lock (Part A) — must NOT import provisioners
├── provisioners.py    # collection + sibling add-on config coercion — must NOT import locks
├── config.json        # defaults (§4)
├── config.md          # human-readable config docs
└── manifest.json      # {"package": "ci_buddy", "name": "...", ...}
```

- **Relative imports only** (AnkiWeb installs use the numeric add-on id as the folder
  name; absolute imports break).
- **Load order is alphabetical by folder name.** If another baked add-on re-enables a
  locked action, ensure `ci_buddy` sorts after it (or re-apply on `profile_did_open`).
- Pure-logic helpers (config parsing, the action allow/deny map, the sibling-config
  plan) should be plain functions with **no `aqt` import** so they're
  unit-testable directly.

## 7. Packaging & CI
- `package.sh` / `make package`: zip `ci_buddy/` (with `manifest.json` at root) into
  `ci_buddy.ankiaddon`. No wheels, no vendor step.
- Deliverable for headless-anki: the `.ankiaddon` or the raw `ci_buddy/` dir to drop into
  `/data/addons21/ci_buddy/`.
- CI (GitHub Actions): build the package + run tests (§8). Mirror the AnkiMCP repo's CI
  conventions where reasonable.

## 8. Testing
Pure-GUI locking is hard to unit-test without a running Anki → lean on an **E2E test in
headless-anki over VNC**, asserting:

**Lock:**
1. `mw.form.actionPreferences.isVisible()` (or `.isEnabled()`) is `False`; same for
   Add-ons. Switch Profile stays **visible** (multi-profile support, v0.7.0), while the
   profile-manager screen's Open Backup / Quit / Downgrade buttons are locked.
2. `aqt.dialogs.open("Preferences", mw)` → dialog `not isEnabled()`; `open("AddonsDialog",
   mw)` likewise.
3. **Regression guard:** `open("AddCards", mw)` and `open("Browser", mw)` produce
   **enabled** windows (catches an over-broad dialog filter).
4. `mw.addonManager.writeConfig("some.module", {...})` then re-read → unchanged.
5. Note Types stays enabled.

**Config provisioners** (unit-testable with fake `col` / `addonManager` objects — no
real Anki needed):
6. `ensure_latex_generation` → `RENDER_LATEX` forced on at `collection_did_load` and
   after `sync_did_finish`; idempotent (no write when already on).
7. `hide_ankimcp_*` → the gated AnkiMCP config keys forced false in one
   `getConfig`/`writeConfig` pass, through the Seam-4 shim's preserved original;
   safe no-op when AnkiMCP is absent.

Where a headless assertion is impractical, document a manual VNC checklist — don't claim
coverage you don't have.

## 9. Acceptance checklist
- [ ] Preferences, Add-ons gone/disabled per config; Switch Profile usable with the
      profile-manager screen's dangerous buttons locked; `writeConfig` neutered.
- [ ] Add / Browse / Study / Decks / Stats / Note Types still open and work.
- [ ] Programmatic `dialogs.open("Preferences"|"AddonsDialog")` disabled; other dialogs unaffected.
- [ ] ci-buddy never reads/writes/clears any sync credential (Part B): a manual AnkiWeb
      login over VNC survives profile close and pod restart (persisted on the PVC).
- [ ] ci-buddy never triggers sync itself; no k8s creds in the add-on.
- [ ] No exception path can crash Anki startup or block the collection.
- [ ] Packaged as `.ankiaddon`; bakes cleanly into a headless-anki image.
- [ ] Tests (or a documented manual checklist) cover lock + keep-enabled guards + the
      config provisioners.

---

## Appendix — key aqt/anki reference points (compiled against 25.9.2, not re-verified against 26.5 — verify by symbol)
- `actionPreferences` → `main.py onPrefs` → `dialogs.open("Preferences", mw)`;
  `Preferences(QDialog)` in `preferences.py` (sync login/logout + custom URL live here).
- `dialog_manager_did_open_dialog` — fires from `aqt/dialogs.py open()` (create + reactivate);
  serves the whole registry → mandatory name filter.
- `addons_dialog_will_show(dialog)` — `aqt/addons.py`; the NixOS lock seam.
- `AddonManager.writeConfig` — `aqt/addons.py`; persists to `meta.json`.
- `actionSwitchProfile` → `unloadProfileAndShowProfileManager` (`main.py`).
- `action_upgrade_downgrade` — auto-hidden when no launcher (common in PyPI/headless).
- `top_toolbar_did_init_links` — strip the `sync` link (`toolbar.py`); native auto-sync in
  `main.py` on open/close.
- `mw.pm.sync_auth()` — `profiles.py`; re-reads the profile per call, so the AnkiMCP sync
  picks up a fresh VNC login without a restart (Part B cross-repo note).
- Load order: `allAddons` sorts folders alphabetically.
