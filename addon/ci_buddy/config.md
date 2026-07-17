# ci-buddy configuration

ci-buddy has two independent, config-gated modules:

- **GUI lock** (Part A) — makes Anki a locked appliance.
- **Config provisioners** — force managed-environment config the locked GUI can
  no longer reach: collection-level LaTeX rendering, and hiding the sibling
  AnkiMCP add-on's UI surfaces.

ci-buddy **never touches AnkiWeb sync credentials**: the user logs into AnkiWeb
manually inside Anki (over the VNC remote desktop), and — exactly like desktop
Anki — that login persists in `prefs21.db` on the per-user persistent volume
across pod restarts/sleep. It is wiped only when the instance (and its PVC) is
deleted. (Earlier revisions injected an hkey from a mounted credentials file;
that credential-provisioning channel was removed in v1.)

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
| `lock_profile_switch` | `false` | File → Switch Profile (`actionSwitchProfile`). **Unlocked by default since v0.7.0** — hosted users get multiple Anki profiles, so Switch Profile must stay usable. The screen it opens is locked down separately (see *Profile-manager screen* below). |
| `lock_upgrade_downgrade` | `true` | Tools → Upgrade/Downgrade (`action_upgrade_downgrade`; usually already auto-hidden on headless/PyPI installs). |
| `lock_check_for_updates` | `true` | Tools → Check for Updates (`action_check_for_updates`). The action only exists on Anki **26.05+** (25.07–25.09 only have `action_upgrade_downgrade`; ≤25.02 has neither) — on versions without it the lock is skipped with a logged warning, never a crash. |
| `lock_note_types` | `false` | Tools → Manage Note Types (`actionNoteTypes`). **Kept enabled by default** — editing templates/CSS is an explicit product decision. |
| `lock_import` | `true` | File → Import (`actionImport`). Destructive (can replace the collection) → locked by default. |
| `lock_export` | `true` | File → Export (`actionExport`). Locked by default in the managed environment. |
| `lock_create_backup` | `true` | File → Create Backup (`action_create_backup`). Locked by default (backups are the platform's job). |
| `lock_open_backup` | `true` | File → Open Backup (`action_open_backup`). Destructive → locked by default. |
| `lock_exit` | `true` | File → Exit (`actionExit`). Exiting Anki inside the pod just triggers a supervisor restart loop → locked by default. |
| `lock_database_check` | `false` | Tools → Check Database (`actionFullDatabaseCheck`). Low-risk maintenance → unlocked by default; toggle to lock. |
| `lock_file_menu` | `false` | The **entire File menu** (`menuCol`: Switch Profile / Import / Export / Create Backup / Open Backup / Exit). A QMenu is not a QAction, so it is locked via `menu.menuAction()` — `setVisible(False)` under `"hide"`, `setEnabled(False)` under `"disable"`. **Unlocked by default since v0.7.0** (multi-profile support): with the default per-item locks above, the File menu shows **only Switch Profile**. Turning this on subsumes all the per-item File locks (including Switch Profile). |

Every `mw.form` attribute above is accessed with `getattr(..., None)`: a
missing/renamed action or menu on **any** Anki version is skipped with a logged
warning — an outdated map entry can never crash startup.

### Profile-manager screen (Part A, Seam 9)

The screen File → Switch Profile opens (and the profile picker shown at startup
when no profile auto-loads) is built from aqt `forms/profiles.ui` and carries
buttons that must not be reachable in the managed environment:

| Key | Default | Locks |
|---|---|---|
| `lock_profile_manager_open_backup` | `true` | The **Open Backup** button (`openBackup`) — destructive collection restore, the same surface as File → Open Backup. |
| `lock_profile_manager_quit` | `true` | The **Quit** button (`quit`) — exits Anki inside the pod, which just triggers a supervisor restart loop. |
| `lock_profile_manager_downgrade` | `true` | The **Downgrade & Quit** button (`downgrade_button`) — schema downgrade plus quit. |

The **Open / Add / Rename / Delete** buttons (`login` / `add` / `rename` /
`delete_2`) are **never touched**: hosted users manage their own profiles,
including delete (product decision, v0.7.0). Locked buttons respect the global
`hide_vs_disable` choice.

> **Known limit.** Hiding `quit` doesn't close every exit path: the picker
> window's own WM close button still runs `closeEvent → cleanupAndExit`
> (`qt/aqt/main.py:293-300`) — the same class of gap as `lock_exit` not
> blocking the main window's X. Supervisor restart tolerance covers it.

**Timing / mechanism.** `AnkiQt.showProfileManager` rebuilds the screen
**fresh on every call** (it assigns a new window and a new
`aqt.forms.profiles.Ui_MainWindow` to `mw.profileDiag` / `mw.profileForm` —
`qt/aqt/main.py:335-336`), so a one-shot lock would silently unlock on the
next showing. ci-buddy therefore wraps `mw.showProfileManager` **at add-on
load time** (same load-time precedent as Seam 7): add-ons import inside
`AnkiQt.__init__` (`setupAddons`, main.py:212) *before* `setupProfile` runs
and can show the startup picker (main.py:230-234, 327-328), and
`main_window_did_init` only fires *after* that first showing — so a hook-time
install would be too late. The wrapper calls the original, then locks the
freshly built buttons; every show path (startup picker, File → Switch
Profile via `unloadProfileAndShowProfileManager`, and the
collection-load-failure fallback) goes through `self.showProfileManager`, so
all showings are covered. Fail-open: a missing/renamed button — or a future
Anki that reshapes `profiles.ui` or `showProfileManager` — degrades to
"buttons visible" with a logged `lock skipped` warning, never a startup
failure.

### Collection editing always stays usable

Add, Browse, Study/Review, Decks, Stats, per-deck options, Undo/Redo, and Manage
Note Types are **never** disabled by ci-buddy. The programmatic-open guard
matches the dialog names `Preferences` and `AddonsDialog` **exactly** — it does
not touch AddCards, Browser, EditCurrent, FilteredDeckConfigDialog or DeckStats.

## Security hardening (Part A, Seam 6)

| Key | Default | Meaning |
|---|---|---|
| `lock_debug_console` | `true` | Disables Anki's built-in Python debug console (the `Ctrl+:` / `Ctrl+Shift+;` REPL, an arbitrary-code-execution surface reachable over the noVNC desktop). No-ops `aqt.debug_console.show_debug_console` and disables the main-window `QShortcut` that opens it. Locked by default on the hosted appliance. |

## Update checks (Part A, Seam 7)

| Key | Default | Meaning |
|---|---|---|
| `disable_update_checks` | `true` | Forces the **global** automatic update checks off via `mw.pm`: `set_update_check(False)` suppresses the startup "a new version of Anki is available" prompt, and `set_check_for_addon_updates(False)` suppresses the 24h-throttled add-on update fetch. Both flags live in `pm.meta` (`prefs21.db`, global — not per-profile). Applied at **add-on load time** every launch — the automatic checks fire inside `loadProfile`, *before* `main_window_did_init`, so a hook-time force would be too late; and re-forcing each launch means a value re-enabled via Preferences only survives until the next boot. `set_check_for_addon_updates` only exists on Anki **26.05+**: on ≤ 25.09 only the app-update prompt is suppressed — the add-on-update check has no `pm` gate there and stays active — and the missing setter is skipped with a logged warning each boot, never a crash. |

## About dialog debug info (Part A, Seam 8)

| Key | Default | Meaning |
|---|---|---|
| `sanitize_copy_debug_info` | `true` | Help → About stays fully visible and stock (credits, AGPL notice, and the **Copy Debug Info** button all remain), but clicking the button copies the harmless placeholder `"Debug info is disabled in this managed environment."` (plus a trailing newline when any add-on config is dirty — effectively always on the hosted image) instead of the real `supportText()` output + installed add-on list — environment data that would otherwise leak from the managed deployment. Mechanism: rebinds the `supportText` / `addon_debug_info` module globals **inside `aqt.about`** — the names the button's click handler resolves at click time — so it works however About is opened (Help menu via the dialog-manager registry, or a direct `aqt.about.show(mw)` call). Scoped to About only: error reports (`aqt.errors`) resolve their own `supportText` binding and keep the real text. Fail-open: a missing/renamed symbol on any Anki version is skipped with a logged warning — About keeps working and the button then copies the real debug info, never a crash. |

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

## Collection config provisioning

| Key | Default | Meaning |
|---|---|---|
| `ensure_latex_generation` | `true` | Forces the collection boolean `Config.Bool.RENDER_LATEX` **on**. Anki disables LaTeX image generation by default since 24.06 (security), so `[latex]`/TikZ cards otherwise show "LaTeX image generation is disabled in the preferences" and never compile. Since Preferences is locked on the hosted appliance, ci-buddy sets it in code — on `collection_did_load` **and** after every sync (`sync_did_finish`), because the value synced down from AnkiWeb may be off. Idempotent: it only writes when the flag is currently off, so it never re-dirties the collection. |

> **Note.** `ensure_latex_generation` only flips the global render flag. Rendering
> TikZ additionally requires the note type to be in **SVG** mode with a
> `tikz` + `dvisvgm` LaTeX preamble — that is note-type/collection data, not an
> add-on setting, and is out of scope here.

## Sibling add-on coordination

| Key | Default | Meaning |
|---|---|---|
| `hide_ankimcp_toolbar_indicator` | `true` | Forces the AnkiMCP server add-on's `show_toolbar_indicator` config key **off**, hiding its persistent `[• AnkiMCP]` button from Anki's top toolbar in the managed environment. |
| `hide_ankimcp_settings_menu_item` | `true` | Forces the AnkiMCP server add-on's `show_settings_menu_item` config key **off**, hiding its *AnkiMCP Server Settings…* entry from Anki's Tools menu in the managed environment. |

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

## Anki compatibility warning (Seam 10)

| Key | Default | Meaning |
|---|---|---|
| `warn_unsupported_anki` | `true` | Warns — **loudly, but without blocking anything** — when ci-buddy runs on an Anki version that is not in its hardcoded verified matrix (`SUPPORTED_ANKI_VERSIONS` in `core.py`). |

ci-buddy is baked into the managed image together with a pinned `aqt`, and every
lock seam above is **fail-open by design** — so on an unverified Anki the locks
can silently degrade with no visible symptom. Seam 10 is the tripwire for the
operator's manual image sanity check. At add-on load time it reads
`anki.buildinfo.version` (the same runtime source Anki's own
`anki.utils.version_with_build` uses) and compares it — **exact string match
only**, no ranges or prefixes — against the matrix. A missing/unreadable
`buildinfo` counts as unsupported. On a mismatch:

- **stderr (pod logs):** a multi-line warning, every line carrying the stable
  greppable marker `CI_BUDDY_UNSUPPORTED_ANKI`, stating the found Anki version,
  the ci-buddy version (read from `manifest.json`), the supported list, and the
  instruction to verify all lock seams and add the version to
  `SUPPORTED_ANKI_VERSIONS` in `core.py`.
- **Deck browser:** a big red bold banner prepended above the deck list
  (`deck_browser_will_render_content`) — the landing screen of the VNC sanity
  check.
- **Top toolbar:** a persistent red bold "⚠ unverified Anki" badge appended to
  the toolbar links (`top_toolbar_did_init_links`), so the warning stays visible
  on every screen; the full sentence is in its tooltip.

**This is a warning, NOT a gate.** Anki still starts, and every seam above
still applies normally (fail-open as usual) — nothing is blocked or disabled by
Seam 10. On a **supported** version the seam is completely silent: no output,
no hooks registered.

**Operator workflow.** Bump the image's pinned `aqt` → boot the candidate image
→ the warning shows during the VNC sanity check → manually verify every lock
seam against the new Anki → add its exact `anki.buildinfo.version` string to
`SUPPORTED_ANKI_VERSIONS` in `core.py` → release a new ci-buddy. Mind the
format: PyPI `anki==26.5` declares `version = '26.05'` (zero-padded month) —
the matrix pins the buildinfo spelling, not the pip pin.

Fail-open like everything else: a future Anki that removes/reshapes either hook
or the deck-browser content object degrades to "no banner" with a logged
`warning surface skipped` / `banner skipped` line (the stderr warning has
already been printed by then), never a crash. The single config key gates the
whole feature — stderr and both GUI surfaces.
