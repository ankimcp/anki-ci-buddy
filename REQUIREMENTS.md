# anki-ci-buddy — Technical Requirements

Implementation spec for a small Anki add-on that (A) **locks the GUI** of a hosted / CI
Anki instance and (B) **provisions AnkiWeb sync credentials** so sync works despite the
lock. Written for an implementing agent. Target: **PyPI `aqt` 26.5+, Python 3.12+**
(the versions [headless-anki](../headless-anki) ships). Line numbers below are guidance,
not contracts — resolve symbols by name.

The add-on is **service-context software** (not the general-purpose AnkiMCP add-on), so
service-branded messaging like *"Managed environment — locked"* is acceptable here. Keep
brand strings in one constant.

---

## 1. Goal & scope

`anki-ci-buddy` is **one add-on with two independent, config-gated modules**:

- **`locks.py`** — make Anki a locked appliance (no add-on install/remove/config, no
  Preferences, no profile switching, no app upgrade), while collection editing and Note
  Types stay fully usable.
- **`provisioning.py`** — inject the AnkiWeb **sync key (hkey)** + endpoint into the
  profile so sync works even though the GUI's only login surface (Preferences) is locked.
  `provisioning.py` MUST NOT import `locks.py` (keep the two concerns decoupled).

**In scope:** the GUI lock; reading a credentials file and applying it via
`mw.pm.set_sync_key`; picking up refreshed credentials fast; clearing the key on teardown.

**Out of scope (other repos — do NOT build here):**
- **Sync execution.** The **AnkiMCP server add-on** owns all sync via an async job model
  (`sync()` → `job_id` → poll → `resolve`). ci-buddy only seeds the *credential*.
- **Password custody / the `user+pass → hkey` exchange.** A dashboard-side helper (or the
  k8s operator) holds the password encrypted and mints the hkey. The password NEVER
  enters the pod.
- **Credential *delivery* into the pod** (the k8s Secret, the operator, any
  Secret-watching sidecar). That is the operator / headless-anki repos' job. ci-buddy is
  **agnostic to delivery** — it only reads a local file at a configured path (see §B).
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
| `actionSwitchProfile`         | File → Switch Profile    | YES   |
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
> ensure `provisioning.py` reads its config *before* the shim would interfere (it only
> blocks writes, so reads are fine — but read early to be safe).

### Seam 5 (config-gated): sync surfaces
Sync is **not** a menu item. For the hosted deployment (deterministic operator-driven
sync only), strip the top-toolbar sync link via `top_toolbar_did_init_links` (filter the
link whose id is `sync`) and disable native auto-sync (§B.6).

> ⚠️ **Ship-default vs hosted-default mismatch.** The add-on's *own* config defaults for
> `strip_sync_link` / `disable_native_auto_sync` are conservative (`false`) so a
> non-hosted user isn't surprised. The **hosted image must flip these to `true`** in its
> mounted config. Say so loudly in config.md — otherwise someone ships the add-on default
> and gets uncontrolled auto-sync.

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

# Part B — Provisioning (sync credential injection)

## B.1. Credential model (fixed — don't reopen)
- A trusted control-plane component (dashboard helper **or the k8s operator**) holds the
  user's AnkiWeb username+password (encrypted), calls `sync_login(user, pass)` → a
  long-lived **hkey** (bearer token), and **delivers only the hkey** to the pod.
- **The password never enters the pod.** Rationale: a leaked pod exposes a *token*, not
  the AnkiWeb password.
- ⚠️ **Threat sizing (be honest in docs):** AnkiWeb's hkey is **account-scoped, not
  per-device** — there is no granular revoke; the only kill switch is a password change,
  which invalidates every device's session. A leaked hkey grants **full read/write on the
  collection** until the user rotates their password. Treat anything the hkey touches
  (including `prefs21.db`, see §B.5) as **password-adjacent**.
- The pod **cannot self-refresh** (no password on-pod). Refresh always originates
  control-plane-side; ci-buddy's only job is to *pick up* the new value fast.

## B.2. The credentials file — the contract ci-buddy publishes
ci-buddy reads a single JSON file at a **config-driven path** (§4, `credentials_path`,
default e.g. `/run/ankimcp/sync-credentials.json`). Whoever delivers it (operator /
sidecar / plain Secret mount) MUST honor this contract:

```json
{ "v": 1, "serial": 7, "hkey": "…", "endpoint": "https://sync.example.com/", "username": "user@example.com" }
```

- **`serial`** (monotonic int): ci-buddy re-injects **only when `serial` increases** —
  NOT by diffing the hkey string. This detects rollback and makes "changed?" unambiguous.
- **`endpoint`** optional → omit for default AnkiWeb. **`username`** display-only.
- **Writer must write atomically** (`tmp + os.replace`) so ci-buddy never reads a partial
  file. **Reader must survive a parse/IO failure silently and retry next tick** (never
  crash, never block the collection).
- **Perms `0400`, owned by the Anki uid.** A root-owned `0400` mount is unreadable by the
  app — the deployment must use `fsGroup`/`defaultMode` (or a sidecar that writes as the
  Anki uid). Note this to headless-anki (§C).

## B.3. Injection behavior
On `profile_did_open` **and** on a short **mtime-poll `QTimer`** (default 1–2s), read the
file; if `serial` is newer than the last applied serial:
1. `mw.pm.set_sync_key(hkey)` — mutates the in-memory profile; `sync_auth()` re-reads it
   per call, so the **next** MCP `sync()` uses the new key with no restart.
2. If `endpoint` present and passes the allowlist (§B.4): apply the custom-sync-URL setter
   (resolve exact symbol; `mw.pm` endpoint helpers). If absent: revert to default
   AnkiWeb (clear any previously-set custom URL).
3. Set `set_sync_username(username)` — some UI surfaces and the media-sync path key off
   logged-in state; also makes the locked toolbar coherent.
4. `mw.pm.save()` so a crash doesn't revert to a stale/dead key.
5. Record the applied `serial`.

**Guards:** run on the Qt main thread only (the QTimer already is). Guard every injection
with `mw.pm is not None and mw.pm.profile is not None` (the timer can fire during
profile open/close transitions). Absent file → no-op + one warning (supports non-service /
local use). Re-inject on **every** `profile_did_open`, not just the first.

## B.4. Endpoint is an exfil vector — allowlist it
Whoever writes the file could point the hkey at a rogue sync server. The writer is
trusted, but cheap hardening: if `endpoint` is present, **allowlist its scheme+host** in
the add-on (config `endpoint_allowlist`, default = AnkiWeb hosts); reject → log + ignore
the endpoint (keep default). If default AnkiWeb, omit the field and skip the setter.

## B.5. Teardown — clear the key
`prefs21.db` becomes **credential-bearing** once `set_sync_key` persists — any snapshot /
backup of the profile volume between sessions now carries the hkey. On
`profile_will_close`, **clear the sync key** (`set_sync_key(None)` / empty + `save()`).
Re-injection on `profile_did_open` makes this nearly free, and it keeps the at-rest
profile volume clean. (Config `clear_key_on_close`, default `true`.)

## B.6. Native auto-sync — default OFF
Anki auto-syncs on profile open and on close. In the hosted model, sync is **deterministic
and operator-driven** (MCP `sync()` at chosen moments, e.g. a k8s `preStop` hook), so
native auto-sync must be **off** (`disable_native_auto_sync`, hosted-default `true` — see
Seam 5's ship-vs-hosted note). ci-buddy **does NOT trigger sync itself** — that would be a
second sync engine competing with the MCP one. Single sync executor = the AnkiMCP add-on.

## B.7. Cross-repo dependency (verified — keep it true)
The refresh loop only works if the AnkiMCP sync job builds `SyncAuth` **fresh per run**
from `mw.pm.sync_auth()` and surfaces auth failure **structurally** (`code="auth_failed"`)
via the backend path — never through aqt's interactive login dialog (which, behind the
Preferences lock, would brick the session). ✅ **As of the current AnkiMCP add-on this
holds** (`_sync_runner.py`: fresh `sync_auth()` at each `start_sync`/`resolve_sync`;
`SyncErrorKind.AUTH → auth_failed`; no `mw.onSync()`, no dialogs). If that add-on is ever
refactored to cache a `SyncAuth`, refresh silently breaks — call this out as a regression
guard.

## B.8. Redaction
ci-buddy **must never log the hkey** — log a fingerprint (first 8 of `sha256`) only. The
AnkiMCP add-on's `file_log` redaction registry does **not** cover a secret held by a
*different* add-on, so ci-buddy needs its own discipline.

---

## 4. Config surface (`config.json`)

Every lock and provisioning behavior is individually toggleable. Proposed **add-on
defaults** (conservative — the hosted image overrides the flagged ones):

```json
{
  "managed_by_label": "Managed environment — settings are locked",
  "hide_vs_disable": "hide",

  "lock_preferences": true,
  "lock_addons": true,
  "lock_addon_config_writes": true,
  "lock_profile_switch": true,
  "lock_upgrade_downgrade": true,
  "lock_note_types": false,
  "lock_import": true,
  "lock_open_backup": true,
  "lock_database_check": false,
  "strip_sync_link": false,               // hosted image → true

  "provisioning_enabled": false,          // hosted image → true
  "credentials_path": "/run/ankimcp/sync-credentials.json",
  "credentials_poll_seconds": 2,
  "endpoint_allowlist": ["ankiweb.net", "ankiuser.net"],
  "clear_key_on_close": true,
  "disable_native_auto_sync": false       // hosted image → true
}
```

- `hide_vs_disable`: `"hide"` → `setVisible(False)`; `"disable"` → `setEnabled(False)`.
- Unknown/typo keys → `print()` a warning (visible in container logs), never crash.

## 5. Non-goals / guardrails
- Do **not** disable any collection-editing surface (§A.4). When in doubt, do nothing.
- Do **not** implement filesystem read-only locking here (§A.2, §C.1).
- Do **not** hardcode a dashboard URL, do the password↔hkey exchange, drive sync, or reach
  the k8s API. ci-buddy reads a local file and calls Anki APIs — nothing more.
- **Fail open for the collection, closed for settings:** any exception in a lock or
  provisioning handler must be caught and logged, never crash startup or block the
  collection.

## C. Interaction with headless-anki / the operator (deployment constraints)
The implementing agent should surface these to the operator / headless-anki repos; they
are *their* responsibility but ci-buddy's correctness depends on them:

1. **Why the shim, not FS perms (§A.2):** Anki writes inside the add-on dir at runtime —
   config → `<addons21>/<module>/meta.json`; add-ons keep `user_files/` *inside* their own
   dir (the AnkiMCP add-on writes `user_files/credentials.json`, `user_files/ankimcp.log`).
   A blanket read-only `addons21` mount breaks these and first-run `meta.json` creation.
2. **Credential delivery + the "seconds" refresh (operator/sidecar, NOT ci-buddy):** the
   operator owns the pod lifecycle and credential lifecycle. The out-of-pod operator
   cannot write into a running pod's filesystem fast — the kubelet Secret-mount path is
   ~60s and `kubectl exec` is a fragile, over-privileged hack. To hit **seconds**, the
   operator injects a **tiny Secret-watching sidecar** into each pod that writes the
   credentials file atomically into a shared `emptyDir` on the sub-second k8s *watch*
   event. ci-buddy just reads that file (§B.3) — **agnostic to whether it's a directly
   mounted Secret or a sidecar-fed emptyDir**, so this is purely an infra decision.
   - Keep the k8s API credential in the **sidecar**, never in the Anki container (which
     runs the customer-facing MCP server + VNC). ci-buddy holds **no** k8s creds.
   - Scope the sidecar's Role with `resourceNames:` to exactly the one Secret.
   - **Mount the Secret as a whole volume, never `subPath`** — `subPath` mounts are frozen
     at creation and never update, silently killing refresh.
3. **Durability — `preStop` sync is NOT a safety net.** A `preStop` MCP sync is bounded by
   `terminationGracePeriodSeconds` (media-heavy syncs can be SIGKILLed mid-flight) and
   never runs on OOM/eviction/node-loss. With native auto-sync off, an ungraceful pod
   death loses everything since the last operator sync. The **real** safety net is `/data`
   on a **per-user persistent volume** (and/or periodic operator-driven syncs); `preStop`
   sync is best-effort polish on top.
4. Profile-level state (`prefs21.db`) lives outside the add-ons dir. Once ci-buddy injects
   the hkey it is credential-bearing (§B.5) — treat that volume as secret-grade or rely on
   `clear_key_on_close`.

## 6. Add-on structure
Tiny, dependency-free (stdlib + `aqt`/`anki` only — no vendoring, no `user_files`).

```
ci_buddy/
├── __init__.py        # entry: register hooks, install writeConfig shim, start provisioning
├── locks.py           # GUI lock (Part A) — must NOT import provisioning
├── provisioning.py    # credential injection (Part B) — must NOT import locks
├── config.json        # defaults (§4)
├── config.md          # human-readable config docs (note the hosted-image overrides!)
└── manifest.json      # {"package": "ci_buddy", "name": "...", ...}
```

- **Relative imports only** (AnkiWeb installs use the numeric add-on id as the folder
  name; absolute imports break).
- **Load order is alphabetical by folder name.** If another baked add-on re-enables a
  locked action, ensure `ci_buddy` sorts after it (or re-apply on `profile_did_open`).
- Pure-logic helpers (config parsing, the action allow/deny map, serial comparison,
  endpoint allowlist check) should be plain functions with **no `aqt` import** so they're
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
   Add-ons, Switch Profile.
2. `aqt.dialogs.open("Preferences", mw)` → dialog `not isEnabled()`; `open("AddonsDialog",
   mw)` likewise.
3. **Regression guard:** `open("AddCards", mw)` and `open("Browser", mw)` produce
   **enabled** windows (catches an over-broad dialog filter).
4. `mw.addonManager.writeConfig("some.module", {...})` then re-read → unchanged.
5. Note Types stays enabled.

**Provisioning** (unit-testable with a fake `mw.pm` — no real AnkiWeb needed):
6. Newer `serial` → `set_sync_key` called with the new hkey; same/older serial → no-op.
7. `endpoint` outside the allowlist → ignored, default kept.
8. Corrupt/partial/absent file → no crash, retried next tick.
9. `clear_key_on_close` → key cleared on `profile_will_close`.
10. hkey never appears in logs (fingerprint only).

Where a headless assertion is impractical, document a manual VNC checklist — don't claim
coverage you don't have.

## 9. Acceptance checklist
- [ ] Preferences, Add-ons, Switch Profile gone/disabled per config; `writeConfig` neutered.
- [ ] Add / Browse / Study / Decks / Stats / Note Types still open and work.
- [ ] Programmatic `dialogs.open("Preferences"|"AddonsDialog")` disabled; other dialogs unaffected.
- [ ] Credentials file: newer `serial` → injected via `set_sync_key` (+ endpoint allowlisted,
      + username, + `mw.pm.save()`); older/equal → no-op; corrupt/absent → safe no-op.
- [ ] Key cleared on `profile_will_close` (when `clear_key_on_close`).
- [ ] Native auto-sync off in hosted config; ci-buddy never triggers sync itself.
- [ ] hkey never logged (fingerprint only); no k8s creds in the add-on.
- [ ] No exception path can crash Anki startup or block the collection.
- [ ] Packaged as `.ankiaddon`; bakes cleanly into a headless-anki image.
- [ ] Tests (or a documented manual checklist) cover lock + keep-enabled guards + provisioning.

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
- `mw.pm.sync_auth()` / `set_sync_key()` / `set_sync_username()` / `save()` — `profiles.py`.
  `set_sync_key` mutates the in-memory profile; `sync_auth()` re-reads per call.
- Load order: `allAddons` sorts folders alphabetically.
