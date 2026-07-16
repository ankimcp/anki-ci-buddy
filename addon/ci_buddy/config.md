# ci-buddy configuration

ci-buddy has two independent, config-gated modules:

- **GUI lock** (Part A) — makes Anki a locked appliance.
- **Provisioning** (Part B) — injects AnkiWeb sync credentials from a file.

Every behaviour below is individually toggleable. Unknown/typo keys are ignored
with a warning printed to the container log (never a crash).

---

## General

| Key | Default | Meaning |
|---|---|---|
| `managed_by_label` | `"Managed environment — settings are locked"` | Label shown as the Add-ons dialog title when it is locked. |
| `hide_vs_disable` | `"hide"` | `"hide"` → locked menu items **disappear** (`setVisible(False)`). `"disable"` → they are **greyed out** (`setEnabled(False)`). Any other value is treated as `"hide"`. |

## GUI lock (Part A)

| Key | Default | Locks |
|---|---|---|
| `lock_preferences` | `true` | Tools → Preferences (`actionPreferences`) + programmatic `dialogs.open("Preferences")`. |
| `lock_addons` | `true` | Tools → Add-ons (`actionAdd_ons`), the whole Add-ons dialog, and programmatic `dialogs.open("AddonsDialog")`. |
| `lock_addon_config_writes` | `true` | Replaces `addonManager.writeConfig` with a shim that silently drops writes (config changes become non-durable). Reads are unaffected. |
| `lock_profile_switch` | `true` | File → Switch Profile (`actionSwitchProfile`). |
| `lock_upgrade_downgrade` | `true` | Tools → Upgrade/Downgrade (`action_upgrade_downgrade`; usually already auto-hidden on headless/PyPI installs). |
| `lock_note_types` | `false` | Tools → Manage Note Types (`actionNoteTypes`). **Kept enabled by default** — editing templates/CSS is an explicit product decision. |
| `lock_import` | `true` | File → Import (`actionImport`). Destructive (can replace the collection) → locked by default. |
| `lock_open_backup` | `true` | File → Open Backup (`action_open_backup`). Destructive → locked by default. |
| `lock_database_check` | `false` | Tools → Check Database (`actionFullDatabaseCheck`). Low-risk maintenance → unlocked by default; toggle to lock. |

### Collection editing always stays usable

Add, Browse, Study/Review, Decks, Stats, per-deck options, Undo/Redo, and Manage
Note Types are **never** disabled by ci-buddy. The programmatic-open guard
matches the dialog names `Preferences` and `AddonsDialog` **exactly** — it does
not touch AddCards, Browser, EditCurrent, FilteredDeckConfigDialog or DeckStats.

## Security hardening (Part A, Seam 6)

| Key | Default | Meaning |
|---|---|---|
| `lock_debug_console` | `true` | Disables Anki's built-in Python debug console (the `Ctrl+:` / `Ctrl+Shift+;` REPL, an arbitrary-code-execution surface reachable over the noVNC desktop). No-ops `aqt.debug_console.show_debug_console` and disables the main-window `QShortcut` that opens it. Locked by default on the hosted appliance. |

## Sync surfaces (Part A, Seam 5)

| Key | Default | Meaning |
|---|---|---|
| `strip_sync_link` | `false` | Removes the sync link from the top toolbar. |
| `disable_native_auto_sync` | `false` | Turns off Anki's auto-sync on profile open/close (sets the in-memory `autoSync` profile flag; not persisted to disk). |

> **Note on the hosted deployment.** An earlier revision of the spec told the
> hosted image to flip `strip_sync_link` / `disable_native_auto_sync` to `true`.
> **That is no longer the case** (platform decision 14): the hosted pod behaves
> like a regular Anki desktop device with native auto-sync **ON**, so these stay
> `false` in the hosted config too. The options remain available for anyone who
> wants a fully deterministic, operator-driven-only sync setup.
>
> Suppression is reliable from the **first** sync: Anki fires the
> `profile_did_open` hook immediately before its open auto-sync check
> (`maybe_auto_sync_on_open_close`), and that check reads the `autoSync` profile
> flag live — so the flag this option sets is already in effect for the very
> first open/close auto-sync of the session.

## Provisioning (Part B)

| Key | Default | Meaning |
|---|---|---|
| `provisioning_enabled` | `false` | Master switch for credential injection. **The hosted image sets this to `true`.** When `false`, ci-buddy registers no provisioning hooks. |
| `credentials_path` | `"/run/ankimcp/sync-credentials.json"` | Path to the JSON credentials file (see contract below). |
| `credentials_poll_seconds` | `2` | How often to poll the file's mtime for changes (floored at 0.5s). |
| `endpoint_allowlist` | `["ankiweb.net", "ankiuser.net"]` | Allowed sync-server hosts (exact host or subdomain). An `endpoint` outside this list is ignored and the default sync server is kept. |
| `clear_key_on_close` | `true` | On `profile_will_close`, clear the sync key from the profile so the at-rest `prefs21.db` carries no credential. Re-injected on next open. |

### Collection config provisioning

| Key | Default | Meaning |
|---|---|---|
| `ensure_latex_generation` | `true` | Forces the collection boolean `Config.Bool.RENDER_LATEX` **on**. Anki disables LaTeX image generation by default since 24.06 (security), so `[latex]`/TikZ cards otherwise show "LaTeX image generation is disabled in the preferences" and never compile. Since Preferences is locked on the hosted appliance, ci-buddy sets it in code — on `collection_did_load` **and** after every sync (`sync_did_finish`), because the value synced down from AnkiWeb may be off. Idempotent: it only writes when the flag is currently off, so it never re-dirties the collection. Independent of `provisioning_enabled`. |

> **Note.** `ensure_latex_generation` only flips the global render flag. Rendering
> TikZ additionally requires the note type to be in **SVG** mode with a
> `tikz` + `dvisvgm` LaTeX preamble — that is note-type/collection data, not an
> add-on setting, and is out of scope here.

### Sibling add-on coordination

| Key | Default | Meaning |
|---|---|---|
| `hide_ankimcp_toolbar_indicator` | `true` | Forces the AnkiMCP server add-on's `show_toolbar_indicator` config key **off**, hiding its persistent `[• AnkiMCP]` button from Anki's top toolbar in the managed environment. Independent of `provisioning_enabled`. |
| `hide_ankimcp_settings_menu_item` | `true` | Forces the AnkiMCP server add-on's `show_settings_menu_item` config key **off**, hiding its *AnkiMCP Server Settings…* entry from Anki's Tools menu in the managed environment. Independent of `provisioning_enabled`. |

These two gates cover independent AnkiMCP UI surfaces; each is applied only if
its own flag is `true`. Both are forced in a **single** `getConfig`/`writeConfig`
pass. Safe no-op if AnkiMCP isn't installed, exposes no config, or every enabled
key is already off (idempotent — never re-writes `meta.json`).

> **How AnkiMCP is located (directory vs. manifest package).** Anki keys
> `addonManager.getConfig`/`writeConfig` by an add-on's on-disk **directory**
> name, which is not stable across installs — on the hosted image AnkiMCP is
> installed under the directory `ankimcp`, while its `manifest.json` still
> declares `"package": "anki_mcp_server"`. ci-buddy therefore does **not** assume
> the directory equals the package: it scans installed add-ons
> (`addonManager.allAddons()`), reads each one's `manifest.json` `package`, and
> uses the matching add-on's directory as the config key. If no installed add-on
> declares package `anki_mcp_server`, ci-buddy logs a distinctive, greppable
> warning (`CI_BUDDY_ADDON_PACKAGE_NOT_FOUND`) and no-ops (fail-open).

> **How it beats the write-lock, and why it takes effect the same session.**
> This is the one place ci-buddy durably writes *another* add-on's config, so it
> must dodge the Seam 4 write shim (`lock_addon_config_writes`), which otherwise
> silently drops all `addonManager.writeConfig` calls. It does so by writing
> through the **original** `writeConfig` that the shim preserves — recovered via
> an attribute the shim stashes on itself, so it works whether the shim is
> installed before or after (robust to registration order) and without coupling
> the two modules.
>
> The write happens at ci-buddy **add-on load time**, not on a GUI hook. AnkiMCP
> reads both `show_toolbar_indicator` and `show_settings_menu_item` exactly once,
> inside its `main_window_did_init` handler, and Anki fires that hook only after
> every add-on has finished importing — so ci-buddy's load-time write lands
> *before* AnkiMCP reads the flags. The surfaces are therefore hidden on the
> **very first** window of the session (not just after a restart, which is what a
> manual config edit would require).

### Credentials file contract

ci-buddy reads a single JSON file (written atomically by the operator / Secret
mount) at `credentials_path`:

```json
{ "v": 1, "serial": 7, "hkey": "…", "endpoint": "https://sync.example.com/", "username": "user@example.com" }
```

- `v` — schema version, must be `1`.
- `serial` — monotonic integer. ci-buddy re-injects **only when it increases**
  (rollback-safe; not diffed by hkey string).
- `hkey` — the AnkiWeb sync key (bearer token). **Never logged** — only a
  sha256 fingerprint (first 8 hex) appears in logs.
- `endpoint` — optional; omit for default AnkiWeb. Must pass the allowlist.
- `username` — optional; display-only.

A corrupt, partial, or absent file is a safe no-op — ci-buddy retries on the
next poll. The hkey grants full read/write on the collection until the AnkiWeb
password is rotated; treat any volume that holds it (including `prefs21.db`) as
password-adjacent.
