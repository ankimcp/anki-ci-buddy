# requirements-headless-anki-image — Hosted Anki Pod Image

Implementation spec for the **hosted Anki pod image** — the container that runs one user's
desktop Anki inside the Anki-as-a-Service platform ([ARCHITECTURE.md](./ARCHITECTURE.md),
component `pod`). Written for an implementing agent. The image **lands in the
[`headless-anki`](../../headless-anki) repo** later; this spec lives here because it is a
platform decision, not an isolated image tweak.

**Target:** Anki via PyPI `aqt==26.5` on Python 3.12 (the version headless-anki already ships
and ci-buddy targets — [../REQUIREMENTS.md](../REQUIREMENTS.md) §Target). Do not float the pin;
the baked add-ons and the add-on-download URL scheme are version-coupled.

**Authority:** where this doc and ARCHITECTURE.md disagree, ARCHITECTURE.md wins and the
disagreement is a bug in this doc. Shared cross-component values (ports, paths, credentials Secret,
B2 key/prefix, PVC size, cache cap) are fixed in [contracts.md](./contracts.md); this doc restates
them for context and must match it. Unresolved points are collected in §14 (Open questions), not
guessed at inline.

---

## 1. What this image is, and which variant it extends

### 1.1 Base variant: extend `x11-vnc`, not `qt-vnc` or `x11-vnc-addons`

headless-anki ships four images (build chain: `base → {qt-vnc, x11-vnc → x11-vnc-addons}`). The
hosted pod image is a **new fifth variant** that extends **`x11-vnc`**. Justification, tied to
what the platform actually needs:

| Requirement | `qt-vnc` (Qt built-in VNC QPA) | `x11-vnc` (Xvfb + openbox + x11vnc) |
|---|---|---|
| Real window manager for Anki's **modal dialogs** (the stock **sync-conflict dialog** surfaces over VNC — ARCHITECTURE §5) | ✗ single surface, no WM | ✓ openbox manages dialogs/focus |
| Mature RFB server that **websockify/noVNC** front cleanly (ARCHITECTURE §9) | limited | ✓ x11vnc + libvncserver is the standard noVNC target |
| Multi-window (Browser, Add Cards, Stats as separate top-levels — ci-buddy keeps these usable, [../REQUIREMENTS.md](../REQUIREMENTS.md) §A.4) | fragile | ✓ |

`x11-vnc-addons` is explicitly **local-only** (README: "not built in CI") and bakes add-ons
straight into `/data/addons21` — which collides with the PVC mount (see §4). We reuse its
*add-on-download logic* (§3.2) but not its layout. Name the new variant **`hosted`**, tag
`hosted-vX.Y.Z`, published by CI alongside the others.

### 1.2 What the `hosted` variant adds over `x11-vnc`

1. Two baked add-ons: **`ci_buddy`** (GUI lock + sync-credential injection,
   [../REQUIREMENTS.md](../REQUIREMENTS.md)) and the **AnkiMCP server add-on** (MCP transport +
   sync executor).
2. **websockify + noVNC** static assets, to serve VNC-in-the-browser (ARCHITECTURE §9).
3. **rclone** binary + mount wiring for the B2 media store (ARCHITECTURE §4), with an
   **internal-mount / external-mount switch** (§5.3).
4. A **managed add-on overlay** bootstrap that reconciles the image's add-ons onto the PVC on
   every start (§3.3) — the resolution of the "baked yet on-PVC" tension.
5. A **profile bootstrap + wait-for-media gate + ordered launch** startup (§6), replacing the
   naïve `while true; anki -b /data` loop.
6. A **`preStop` graceful-quit + rclone-drain** hook for the ~~activator~~-driven downscale
   **(2026-07-11: lifecycle-service-driven — ARCHITECTURE §16.2)** (ARCHITECTURE §5, §7).

Everything customer-facing (Anki, the MCP add-on, VNC) runs **unprivileged, with no Kubernetes
credentials** (ARCHITECTURE §3, §10 Layer 2). The FUSE privilege question is **decided
(2026-07-10)**: per-user **privileged rclone sidecar** — see §8.4 / ARCHITECTURE §12 item 9.

---

## 2. Software inventory

Layered on `x11-vnc` (which already brings `python:3.12-slim`, `aqt==26.5`, Qt6/xcb libs,
`Xvfb`, `openbox`, `x11vnc`, `curl`, `jq`, `unzip`, `mpv`, `lame`, locale).

| Component | Source / pin | Why | Notes |
|---|---|---|---|
| **Anki** | `aqt==26.5` (inherited from `base`) | The app | Pin matches ci-buddy target; do not bump silently |
| **ci_buddy add-on** | baked from this repo (`.ankiaddon` or raw `ci_buddy/`) | GUI lock + hkey injection | Hosted config overrides applied (§3.4) |
| **AnkiMCP server add-on** | baked from `anki-mcp-server-addon` | MCP transport + sync executor | Owns `sync()`; ci-buddy only seeds the credential ([../REQUIREMENTS.md](../REQUIREMENTS.md) §1) |
| **rclone** | pinned release (e.g. `v1.6x.x`), verify latest at build | B2 media mount (ARCHITECTURE §4) | Only used in the **internal-mount** variant (§5.3); still installed in both for `rclone rc` drain client |
| **websockify** | pip or distro pkg, pinned | WS↔RFB proxy for noVNC | `websockify --web <novnc> 6080 localhost:5900` |
| **noVNC** | pinned release, static assets in image (`/opt/novnc`) | Browser VNC client (ARCHITECTURE §9) | Self-hosted; CSP-friendly (no CDN) |
| **fuse3 / fusermount3** | distro pkg | rclone mount, clean unmount on drain | Internal-mount variant only |
| `sqlite3` CLI | distro pkg | bootstrap DB inspection (§6.2 — mostly a no-op, see caveat) | |
| `procps` / `curl` | present | health checks, `rclone rc` over HTTP | |

**Add-on IDs / folders:** baked add-ons use **stable folder names** (`ci_buddy`,
`ankimcp` — not numeric AnkiWeb IDs), because we control the layout and ci-buddy load-order
depends on alphabetical sort ([../REQUIREMENTS.md](../REQUIREMENTS.md) §6: `ci_buddy` must sort
**after** any add-on it re-locks; `ankimcp` < `ci_buddy` alphabetically — good, ci_buddy loads
last). Relative imports only inside each add-on.

---

## 3. Add-ons: the "baked yet on-PVC" tension and its resolution

### 3.1 The tension (ARCHITECTURE §4 vs image packaging)

- Anki reads add-ons from `<base>/addons21/` and **writes inside that tree at runtime**: config +
  enabled state → `<addons21>/<pkg>/meta.json`; add-ons keep their own `user_files/` there (the
  AnkiMCP add-on writes `user_files/credentials.json` and `user_files/ankimcp.log`;
  [../REQUIREMENTS.md](../REQUIREMENTS.md) §C.1). So `addons21/` **must be on the writable PVC**
  (ARCHITECTURE §4 places it there explicitly).
- But the add-on *code* ships **in the image**. When the PVC mounts over `/data`, anything baked
  at `/data/addons21` in the image is **hidden by the mount** — the `x11-vnc-addons` approach
  (bake into `/data/addons21`) silently produces an empty add-ons dir on a real PVC.

### 3.2 Ship add-ons in an image stash, not on the mount path

Bake both add-ons into an **image-only stash** outside the PVC mount:

```
/opt/ankimcp/addons/ci_buddy/     ← baked at build time (image layer)
/opt/ankimcp/addons/ankimcp/      ← baked at build time (image layer)
```

`ci_buddy` comes from this repo's `.ankiaddon` (or raw dir). The AnkiMCP add-on is fetched the
same way `x11-vnc-addons` fetches (`https://ankiweb.net/shared/download/{id}?v=2.1&p={ver}` with
`ver` zero-padded per segment, `25.9.2 → 250902`) **or** vendored from source — prefer vendoring
a pinned build so the image is reproducible and offline-buildable. Each stashed add-on carries a
build-stamped `.image_version` marker file.

### 3.3 Bootstrap = **managed add-on overlay** (decision — copy-code-preserve-state)

On every container start, before launching Anki, reconcile each stashed add-on onto the PVC:

**For each `<pkg>` in the stash:**
1. If `/data/addons21/<pkg>/.image_version` **equals** the stash's → skip (warm resume,
   fast path, no writes).
2. Otherwise **overlay the code**: copy every file from the stash into `/data/addons21/<pkg>/`,
   **overwriting code + `config.json`**, but **excluding `user_files/`** (preserve runtime
   state) and **not clobbering** a user's `meta.json` beyond §3.4. Then write the new
   `.image_version`.
3. Ensure the add-on is **enabled** (`meta.json` `"disabled": false`); create a minimal
   `meta.json` if absent so Anki doesn't treat first-run as broken.
4. `chown` the tree to the Anki uid.

**Why this strategy (and not the alternatives):**

| Strategy | Verdict |
|---|---|
| **copy-if-missing** (only when absent) | ✗ never upgrades — fleet-wide image rollouts (ARCHITECTURE §8) would ship code that never reaches existing PVCs |
| **always-refresh-everything** (wipe + copy) | ✗ destroys `user_files/` → loses AnkiMCP's `credentials.json`/logs and any add-on runtime state |
| **managed overlay** (copy code + `config.json`, preserve `user_files/`, version-gated) | ✓ upgrades cleanly on image bump, preserves per-user runtime state, idempotent/cheap on warm resume |

This is the same shape as an OS package manager overlaying `/usr` while leaving `/var` alone.

### 3.4 Config for managed add-ons is **image-owned**

The hosted image must flip ci-buddy's conservative ship-defaults to hosted values
([../REQUIREMENTS.md](../REQUIREMENTS.md) §4, Seam 5 warning). Bake a `config.json` in the
stashed `ci_buddy/` with:

```json
{
  "strip_sync_link": false,
  "disable_native_auto_sync": false,
  "provisioning_enabled": true,
  "credentials_path": "/run/ankimcp/sync-credentials.json",
  "clear_key_on_close": true
}
```

> ⚠️ **Deliberate deviation from ci-buddy's hosted-default table.** ARCHITECTURE **decision 14
> supersedes** [../REQUIREMENTS.md](../REQUIREMENTS.md) §B.6/Seam 5: **native auto-sync stays ON**
> and the **sync link stays visible** — the pod behaves like a stock desktop
> (ARCHITECTURE §5). So `strip_sync_link` and `disable_native_auto_sync` are **`false`** here,
> *opposite* the ci-buddy doc's "hosted → true". `provisioning_enabled` is **`true`** (the pod
> gets its hkey from the mounted Secret via ci-buddy). See §9.

Because ci-buddy neuters `writeConfig` at runtime, `meta.json` config never changes after boot.
To guarantee the baked values win, the overlay step (§3.3) **overwrites `config.json` every
start** and **removes any stale `"config"` key from `meta.json`** for the two managed packages
only. Add-on config is thus image-owned; user runtime state (`user_files/`) is PVC-owned.

**AnkiMCP add-on hosted config (added 2026-07-11 — contracts.md §8c, ARCHITECTURE §16.1).** The
stashed `ankimcp/` add-on likewise ships an image-owned hosted `config.json`:

```json
{
  "hosted_mode": true,
  "hosted_credentials_path": "/run/ankimcp/tunnel-credentials.json"
}
```

In `hosted_mode` the add-on auto-connects **outbound** to the SaaS tunnel on profile open, reads
the static bearer token + `tunnelUrl` from the mounted file (second data key of the same
credentials Secret, §4.1), and **never auto-reconnects** — all disconnects are terminal in
hosted mode (a restart via `spec.restartedAt` is the reconnect mechanism). Exact key names are
owned by the `anki-mcp-server-addon` spec; the pinned file contract is contracts.md §8c.

---

## 4. Volumes & paths contract

Matches ARCHITECTURE §4 exactly. Every path is env-var-configurable (defaults shown). Two stores,
one rule: **SQLite + add-ons on the PVC (never FUSE); media blobs on the rclone/B2 mount.**

### 4.1 Paths table

| Path (default) | Env var | Backing store | Contents | Notes |
|---|---|---|---|---|
| `/data` | `ANKI_BASE` | **PVC** (Hetzner Cloud Volume, 10GB — min volume size) | Anki base dir (`anki -b /data`) | RWOP, one pod only (ARCHITECTURE §3) |
| `/data/prefs21.db` (+`-wal`/`-shm`) | — | PVC | Profile prefs, **holds hkey** after injection | Credential-bearing at rest ([../REQUIREMENTS.md](../REQUIREMENTS.md) §B.5) |
| `/data/addons21/` | — | PVC | `ci_buddy/`, `ankimcp/` (+ meta.json, user_files) | Reconciled by overlay (§3.3) |
| `/data/<profile>/collection.anki2` (+wal/shm) | `ANKI_PROFILE` (dflt `User 1`) | PVC | Main SQLite DB | **never FUSE** |
| `/data/<profile>/collection.media.db2` (+wal/shm) | — | PVC | Media-index SQLite (mmaps `-shm`) | **never FUSE** |
| `/data/<profile>/collection.media/` → **symlink** | — | → rclone mount | Media blobs | Symlink target = `${MEDIA_MOUNT}/collection.media` |
| `/data/<profile>/media.trash/` → **symlink** | — | → rclone mount | Deleted-media staging | Symlink target = `${MEDIA_MOUNT}/media.trash` — **same mount** as above |
| `/data/.rclone-cache/` | `RCLONE_CACHE_DIR` | **PVC** | rclone VFS write-back + LRU read cache | LRU-capped (§5.2); durability of un-flushed writes (ARCHITECTURE §4) |
| `/media/b2` | `MEDIA_MOUNT` | rclone FUSE (internal) **or** CSI volume (external) | mount root holding `collection.media/` + `media.trash/` | Provided per §5.3 |
| `/run/ankimcp/sync-credentials.json` | `CI_BUDDY_CREDENTIALS_PATH` | Secret mount / emptyDir | hkey delivery ([../REQUIREMENTS.md](../REQUIREMENTS.md) §B.2) | `0400`, owned by Anki uid; whole-volume mount, **never `subPath`** |
| `/run/ankimcp/tunnel-credentials.json` | — (add-on config `hosted_credentials_path`, §3.4) | same Secret mount (second data key — added 2026-07-11) | tunnel bearer token + `tunnelUrl` for the AnkiMCP add-on's outbound tunnel connection ([contracts.md §8c](./contracts.md)) | same volume/perms as above; anki container only, never `envFrom`'d |
| `/home/anki`, `/tmp`, `/tmp/.X11-unix` | — | image / emptyDir | Qt/X runtime, X socket | writable; keep off the PVC |

### 4.2 The two symlinks are load-bearing (ARCHITECTURE §4)

`collection.media/` and `media.trash/` **must resolve to the same filesystem**. Anki deletes
media with `fs::rename(collection.media/X → media.trash/X)`; a mount boundary between them throws
`EXDEV` and breaks every delete. Therefore both are subdirectories **under one rclone mount**
(`${MEDIA_MOUNT}/collection.media`, `${MEDIA_MOUNT}/media.trash`), and the profile-dir entries
are symlinks into them. The startup script (§6) creates both target dirs on the mount (idempotent
`mkdir -p`) and both symlinks (idempotent), verifying they resolve before Anki opens the profile.

**SQLite never touches FUSE** — the DBs stay directly on `/data` (PVC). Object-store FUSE breaks
WAL locking and the `-shm` mmap (ARCHITECTURE §4, decision 2).

---

## 5. rclone wiring

### 5.1 Mount command (internal-mount variant)

```bash
rclone mount "${RCLONE_REMOTE}:${B2_BUCKET}/${B2_PREFIX}" "${MEDIA_MOUNT}" \
  --vfs-cache-mode full \
  --use-server-modtime \                     # REQUIRED — ARCHITECTURE §4 / §12 item 1
  --dir-cache-time 1000h \                    # long — avoid re-LIST churn on the media dir
  --cache-dir "${RCLONE_CACHE_DIR}" \         # on the PVC (durable un-flushed writes)
  --vfs-cache-max-size "${RCLONE_CACHE_MAX:-6G}" \   # default 6G cap on the 10GB PVC, never the full library
  --vfs-cache-max-age "${RCLONE_CACHE_MAX_AGE:-720h}" \
  --vfs-write-back "${RCLONE_WRITE_BACK:-5s}" \
  --dir-perms 0700 --file-perms 0600 \
  --rc --rc-addr 127.0.0.1:5572 --rc-no-auth \   # localhost-only, for the drain check (§7)
  --log-level INFO \
  --daemon=false                              # run in foreground; the startup script supervises
```

**Non-negotiable flags (ARCHITECTURE §4, verified against rclone docs):**
- `--vfs-cache-mode full` — Anki's whole-file read/write pattern needs a real local cache;
  reads avoid repeat egress, writes are async write-back.
- `--use-server-modtime` — **REQUIRED.** Without it, Anki's post-write / post-restart media
  rescan issues **one HEAD per file** (~30k for a 30GB library, ARCHITECTURE §12 item 1); with
  it, modtimes ride the LIST (~30 requests) and stay stable, so no re-hash storm.
- **long `--dir-cache-time`** (`1000h`) — pairs with the above; keeps the synthesized dir cache
  from re-LISTing constantly.
- `--cache-dir` on the **PVC** — write-back is async, so a crash before flush must leave the
  pending file on durable disk; rclone resumes the upload on next start. This is why the cache is
  **not** an `emptyDir` (ARCHITECTURE §4, decision 1/8).
- `--vfs-cache-max-size` LRU cap **default 6G on the 10GB PVC** (leaves headroom for DBs + WAL +
  addons) — never the full media set.

### 5.2 B2 remote config from env / Secret (per-user key)

Remote config comes from **environment variables** (`RCLONE_CONFIG_*`) populated from a **mounted
Secret**, never a checked-in `rclone.conf`. Per-user, prefix-scoped B2 application key
(ARCHITECTURE §10 Layer 2 — reuse the `media-library-b2-provisioning.md` bucket-scoped-key +
per-user-prefix model, **don't invent a second one**):

```
RCLONE_CONFIG_B2_TYPE           = <b2 | s3>          # backend — see Open Q §14.2
RCLONE_CONFIG_B2_ACCOUNT/KEY    = <per-user app key id + key>   # (b2 backend)
# or S3-style: RCLONE_CONFIG_B2_ACCESS_KEY_ID / SECRET_ACCESS_KEY / ENDPOINT
B2_BUCKET                       = <shared bucket>
B2_PREFIX                       = <per-user prefix>   # key is restricted to this prefix
```

- The key grants access to **only this user's prefix** — full pod compromise yields the attacker
  their own media, nothing else (ARCHITECTURE §10 Layer 2).
- The B2 endpoint (`s3.eu-central-003.backblazeb2.com` for the S3 backend) must be allowed by the
  namespace **egress CiliumNetworkPolicy** (ARCHITECTURE §10 Layer 1, `toFQDNs`). Flag to the
  infra/operator repos.
- Backend choice (native `b2` vs `s3`-at-B2-endpoint) is **Open Q §14.2** — align with whatever
  key type `media-library-b2-provisioning.md` already provisions.

### 5.3 Mount vehicle — the `MEDIA_MOUNT_MODE` + `CONTAINER_ROLE` switch

The image works under **both** deployment models via two env vars. `MEDIA_MOUNT_MODE` decides who
provides `${MEDIA_MOUNT}`; `CONTAINER_ROLE` decides whether *this* container is the full Anki boot or
the dedicated rclone sidecar.

| `MEDIA_MOUNT_MODE` | Who provides `${MEDIA_MOUNT}` | Image behavior | Fits |
|---|---|---|---|
| `internal` (default for local/compose) | the image itself runs `rclone mount` (§5.1) | launch rclone, wait for mount live, then symlinks + Anki | local dev / docker-compose single container; the cluster rclone **sidecar** container |
| `external` | someone else presents `${MEDIA_MOUNT}` (the **rclone sidecar** in the same pod, or a CSI driver) | **do not** run rclone mount; **wait** for the externally-provided mount to appear, then symlinks + Anki | cluster **anki** container — stays unprivileged, PSA-clean |

**`CONTAINER_ROLE`** (`anki` default | `rclone-sidecar`): the cluster's decided **sidecar** pod shape
(ARCHITECTURE §10 Layer 3, DECIDED 2026-07-10, [contracts.md §10](./contracts.md)) runs this SAME
image twice in one pod:

- **anki container** — `CONTAINER_ROLE=anki`, `MEDIA_MOUNT_MODE=external`: unprivileged (no
  `/dev/fuse`/`SYS_ADMIN`); sees the media mount via k8s propagation and stays baseline-clean.
- **rclone sidecar** — `CONTAINER_ROLE=rclone-sidecar`, `MEDIA_MOUNT_MODE=internal`: the entrypoint
  short-circuits and `exec`s **only** `rclone-mount.sh` (no X/VNC/Anki). It FUSE-mounts B2 into a
  shared `emptyDir` and the mount propagates to the anki container (k8s `Bidirectional` →
  `HostToContainer`). Because that crosses a container boundary, the mount uses **`--allow-other`**
  (the image's `/etc/fuse.conf` enables `user_allow_other`). The sidecar is **`privileged`** —
  Bidirectional propagation *requires* it (verified; kubernetes/kubernetes PR #117812 to allow it
  with just `SYS_ADMIN` was closed unmerged). The operator renders this via `--media-mount-mode=sidecar`.

The `rclone` binary is present in every mode; in a true CSI **external** deployment the rc endpoint is
not local, so the drain is delegated to the mount provider (§7.4, Open Q §14.4). The startup script
branches on `CONTAINER_ROLE` (sidecar short-circuit, §6.1) and `MEDIA_MOUNT_MODE` (§6.3); everything
downstream (symlinks, Anki, gate) is identical.

---

## 6. Startup sequence

Replaces the `x11-vnc` `while true; anki -b /data` loop with an ordered, gated boot. The script
runs entirely as the **non-root Anki user** (§8.1). Ordering rule: **nothing that depends on the
media mount starts before the mount is verified live.**

### 6.1 Order

**Sidecar short-circuit (first thing):** if `CONTAINER_ROLE=rclone-sidecar`, the entrypoint `exec`s
`rclone-mount.sh` and none of the steps below run (no X/VNC/Anki) — that container is *only* the FUSE
mount (§5.3). Everything below is the `anki` role (and the local single-container path).

```
1. X stack:      Xvfb :99  →  openbox                    (rootless; writes /tmp/.X11-unix)
2. VNC:          x11vnc -display :99 -forever -rfbport 5900 -localhost   (bound to localhost;
                   only websockify fronts it — ARCHITECTURE §9 routes VNC as a WS)
3. noVNC/WS:     websockify --web /opt/novnc 6080 127.0.0.1:5900   (browser entry point)
4. Media mount:  branch on MEDIA_MOUNT_MODE (§5.3):
                   internal → start `rclone mount …` (§5.1)
                   external → assume CSI-provided
5. GATE:         wait-for-media-mount (§6.3) — bounded; fail-closed on timeout
6. Symlinks:     mkdir -p ${MEDIA_MOUNT}/{collection.media,media.trash};
                   ln -sfn into /data/<profile>/                     (§4.2)
7. Add-on overlay: managed overlay reconcile (§3.3)
8. Profile bootstrap: first-run profile creation if absent (§6.2)
9. Anki:         supervised `anki -b /data` (restart-on-crash, guarded by shutdown sentinel §7.1)
10. Ready:       readiness = Anki up AND MCP addon port 3141 accepting (§6.4)
```

x11vnc/websockify may start before the media mount (they don't depend on it) — this lets the
noVNC page render a "starting…" state while media comes up. Anki (step 9) must **not** start
before the gate (step 5) passes, because Anki scans the media dir on profile open.

### 6.2 Profile bootstrap (first run) — and an honest note on SQLite pragmas

- **Profile creation must be non-interactive.** headless-anki today ships a pre-baked
  `prefs21.db` to skip Anki's first-run wizard (its documented gotcha). Reuse that: if
  `/data/prefs21.db` is absent (fresh PVC), seed the baked `prefs21.db` and create the
  `/data/<profile>/` dir. Anki then creates `collection.anki2` on first open.
- **WAL:** Anki opens its DBs in `journal_mode=WAL` itself; WAL is persisted in the DB header, so
  there is nothing for the image to force. A pod OOM/SIGKILL is an *application* crash — WAL
  auto-replays on next open, no corruption, no committed-data loss (ARCHITECTURE §4).
- ✅ **The pragma step is settled (ARCHITECTURE §4 corrected 2026-07-10 to match this doc).**
  `synchronous`/`wal_autocheckpoint` are **per-connection pragmas, not persisted in the DB file**,
  and Anki owns its own connection — a `sqlite3` pragma applied at startup is **overwritten the
  moment Anki opens the DB**, so it is a no-op. WAL (Anki's own persisted default) already gives the
  crash-safety property. **v1 ships NO startup `sqlite3 PRAGMA` step** and accepts Anki's defaults;
  an optional **Anki-side hook** (a tiny addon or `aqt` patch on DB open) is the only correct way to
  set these if ever needed (tracked as optional tuning, §14.3).

### 6.3 wait-for-media-mount gate

Bounded poll (default `MEDIA_MOUNT_TIMEOUT=120s`) until **all** hold:
- `${MEDIA_MOUNT}` is a mountpoint (`mountpoint -q "${MEDIA_MOUNT}"`), and
- it is listable (`ls "${MEDIA_MOUNT}" >/dev/null`), and
- (internal mode) `rclone rc --rc-addr 127.0.0.1:5572 vfs/stats` returns success.

On timeout: **fail closed** — exit non-zero and let the pod crash-loop, rather than open the
collection against a dead media mount (which would surface as broken media everywhere). A
crash-loop is visible and self-heals on the next mount attempt; a silently broken collection is
not.

### 6.4 Readiness definition (what the activator forwards on)

> **2026-07-11 note (contracts.md §7, ARCHITECTURE §16.1):** the activator is shelved and port
> 3141 is **not a Service port in v1** — nothing forwards to it, so the "forward-readiness gate"
> framing below is **moot at the platform level**. The dual check remains valid as the pod's own
> **readiness probe** (it still proves "collection loaded + add-on live", which gates the VNC
> door and wake completion); the platform-level **MCP** readiness signal is now "add-on
> connected to the tunnel" (the tunnel holds MCP requests until that happens). Whether the
> hosted add-on keeps the local 3141 listener purely as a probe target is an image/add-on
> bring-up detail.

The activator holds the user's connection until the pod is **Ready** (ARCHITECTURE §7). "Ready"
for this image is **concrete and dual**:

1. **Anki process is up** (the supervised `anki` PID is alive and past profile-open), **and**
2. **the MCP add-on port `3141` is accepting connections** — a successful TCP connect (or, if the
   AnkiMCP add-on exposes an HTTP health path, a 200 on it — Open Q §14.5). Port 3141 only opens
   after the add-on's `profile_did_open` path runs, so this is a real "collection loaded + add-on
   live" signal, not just "container started".

Expose this as a **container readiness probe** (`tcpSocket: 3141`, or the health path). VNC/noVNC
reachability (5900/6080) is a **liveness** concern for the browser view but is **not** part of the
forward-readiness gate — the MCP transport is what the activator forwards first.

---

## 7. preStop / graceful shutdown (ARCHITECTURE §5)

Triggered by the ~~activator~~ **lifecycle service** on idle-TTL downscale (2026-07-11: it owns
the TTL and the downscale trigger — ARCHITECTURE §16.2; setting `replicas: 0` is the whole
trigger), wired by the operator as **each container's** `lifecycle.preStop`. The image ships
one `prestop.sh`; it is **role-aware** (`CONTAINER_ROLE`) and both containers run it.

**Ordering story (sidecar pod) — deterministic via native sidecars.** The rclone sidecar is a
**native sidecar** (init container, `restartPolicy: Always`), which Kubernetes **terminates AFTER**
the anki container has fully stopped (GA k8s 1.33, on-by-default ≥1.29; cluster targets 1.36). So on
downscale:

1. The **anki container** `preStop` runs first: quit Anki cleanly (close-sync), wait for exit, then
   drain the rclone write-back queue via the rc at `127.0.0.1:5572` — which is the **sidecar's**
   rclone, reachable over the pod-shared loopback and **still alive** (native ordering). Anki has
   stopped writing before this drain completes. The anki container does **not** unmount (it is not
   the mount owner — `is_mounter` is false for `CONTAINER_ROLE=anki`).
2. Then Kubernetes terminates the **sidecar**: its own `preStop` does a final *idempotent* drain
   (queue already empty) and unmounts the FUSE mount it owns.

This avoids the fragile cross-container `preStop` race (parallel `preStop` hooks with no ordering);
the native-sidecar guarantee makes "Anki stops writing before the final drain" true by construction.
Residual: if there are literally zero pending writes at shutdown an early drain-return is harmless —
`preStop` is best-effort polish, durability is guaranteed by cache-on-PVC regardless (§7.5). In the
local **single-container** internal mode one `prestop.sh` does all steps in sequence (it is the
mounter). In a **CSI external** deployment the rc is not local → drain delegated to the provider.

### 7.1 Sequence (per container, role-aware)

```
1. touch ${ANKIMCP_RUNTIME_DIR}/SHUTTING_DOWN   # stop the supervisor relaunching Anki (§6.1 step 9)
2. Quit Anki cleanly                              # so close-time auto-sync runs (native sync ON, §9)
     → bounded wait for the Anki PID to exit     (ANKI_QUIT_TIMEOUT, dflt 60s) — no-op in the sidecar
3. Drain rclone pending uploads                   # poll rclone rc if reachable, bounded (RCLONE_DRAIN_TIMEOUT)
4. Unmount rclone cleanly (ONLY if is_mounter)    # fusermount3 -u ${MEDIA_MOUNT} — sidecar / single-container
5. exit 0
```

### 7.2 Step 2 — clean Anki quit (⚠️ mechanism pending verification)

Native auto-sync is **ON** (decision 14), so a *clean* Anki close runs the close-time sync — that
is the point of quitting gracefully rather than `SIGKILL`. The reliable trigger is **not
guaranteed to be `SIGTERM`**: it is unverified whether headless `aqt` runs its close-time sync on
a bare `SIGTERM`. Preferred, robust mechanism: have the **AnkiMCP add-on expose a shutdown action**
that calls `mw.close()` on the Qt main thread (which runs the normal close path incl. sync), and
have `preStop` invoke it (loopback HTTP/MCP call). Fall back to `SIGTERM` only if verified to run
the close hook. **Mark the exact quit trigger as pending verification** and do not ship a
`SIGKILL`-equivalent that skips close-sync. → Open Q §14.1.

### 7.3 Step 3 — rclone drain (verified command, predicate to confirm on real B2)

Poll the rc API until the write-back queue is empty:

```bash
rclone rc --rc-addr 127.0.0.1:5572 vfs/stats
# drain predicate: .diskCache.uploadsInProgress == 0  AND  .diskCache.uploadsQueued == 0
#   (also surface .diskCache.erroredFiles > 0 as a warning — those won't drain on their own)
```

`vfs/stats` and the `diskCache.{uploadsInProgress,uploadsQueued,erroredFiles}` fields are
confirmed present in current rclone. This resolves ARCHITECTURE **§12 item 2** ("exact rc
command"), with the residual that the **exact predicate should be spot-checked on a real B2
bucket** during image bring-up (ARCHITECTURE §12 item 1's same "verify on real bucket" caveat).
Bound the poll; on timeout, log the residual queue depth and proceed to exit (the cache is on the
PVC — see §7.5).

### 7.4 terminationGracePeriodSeconds

`preStop` is bounded by `terminationGracePeriodSeconds`; a media-heavy close-sync can be slow.
**Recommend `terminationGracePeriodSeconds: 120`** (≥ `ANKI_QUIT_TIMEOUT` + `RCLONE_DRAIN_TIMEOUT`
+ slack), tunable per fleet observation. ⚠️ Native-sidecar caveat: if the anki container consumes the
whole grace period, the sidecar receives `SIGTERM`/`SIGKILL` before its own `preStop` finishes — but
the anki container has **already** drained through the sidecar's rc by then, so the only loss is a
tidy unmount, and cache-on-PVC (§7.5) covers the rest. In a true **CSI external** deployment (rc not
local) the drain is delegated to / coordinated with the CSI mount provider (Open Q §14.4).

### 7.5 What SIGKILL skips, and why that's safe

If the grace period is exceeded (or on OOM/eviction/node-loss), the kernel `SIGKILL`s everything
and steps 2–4 are skipped. This is **safe by design** (ARCHITECTURE §4, §5):
- **Collection:** on the PVC; WAL auto-replays on next open → no corruption, no committed loss.
- **Media writes:** the rclone **cache is on the PVC**; any un-flushed write-back file survives
  and rclone finishes the upload on next start.
- **Cross-device freshness:** the only casualty is *staleness* (the missed close-sync), never
  *loss* — the next resume syncs. Durability never depends on sync (ARCHITECTURE §5).

So `preStop` is **best-effort polish**, not a correctness dependency — exactly the belt-and-
suspenders framing of ARCHITECTURE decision 8.

---

## 8. Security fit (ARCHITECTURE §10)

### 8.1 Non-root, run the whole stack as the Anki user

ARCHITECTURE §10 Layer 3 wants `runAsNonRoot` hardening even though PSA `baseline` doesn't
strictly require it. The current `x11-vnc` variant runs its `startup.sh` **as root** ("needs
Xvfb" per headless-anki CLAUDE.md) — the `hosted` variant must **not**. Xvfb, openbox, x11vnc,
websockify, rclone(internal), and Anki all run fine as an unprivileged user provided
`/tmp/.X11-unix` and `$HOME` are writable. Requirements:
- Fixed **non-root uid** (the baked `anki` user; pin the uid explicitly, e.g. `10001`, so the
  mounted Secret's `0400`/owner and `fsGroup` line up — [../REQUIREMENTS.md](../REQUIREMENTS.md)
  §B.2). Set `USER anki` and run `startup.sh` as that user.
- Pod SecurityContext (operator-set, image-compatible): `runAsNonRoot: true`,
  `allowPrivilegeEscalation: false`, `capabilities: drop: [ALL]`,
  `seccompProfile: RuntimeDefault`. `readOnlyRootFilesystem` is a stretch goal — Anki/Qt write to
  `$HOME` and `/tmp`; if set, mount those as `emptyDir`.

### 8.2 No ServiceAccount token, no k8s creds in the main container

`automountServiceAccountToken: false` (operator-set). The Anki container holds **zero Kubernetes
credentials** (ARCHITECTURE §3, §10 Layer 2; [../REQUIREMENTS.md](../REQUIREMENTS.md) §C.2). Any
Secret-watching sidecar (if used for fast credential delivery) keeps its own scoped Role — never
in this container.

### 8.3 Resource limits (defaults; capacity is Open per §12 item 10)

Anki + Qt WebEngine + Xvfb + x11vnc is memory-hungry. **Default requests/limits** (tune against
ARCHITECTURE §12 item 10 capacity modeling on 3× CX43):

| | request | limit |
|---|---|---|
| CPU | 250m | 1500m |
| Memory | 768Mi | 2Gi |

Limits are noisy-neighbor control on shared nodes (ARCHITECTURE §10 Layer 3).

### 8.4 The FUSE-vs-`baseline` question — DECIDED: per-user privileged rclone sidecar

**DECIDED 2026-07-10 (ARCHITECTURE §10 Layer 3, [contracts.md §10](./contracts.md)): per-user rclone
SIDECAR** in the pod. An `rclone mount` that propagates OUT to the anki container needs
`mountPropagation: Bidirectional`, and **Kubernetes requires a container using Bidirectional
propagation to be `privileged: true`** — `/dev/fuse` + `SYS_ADMIN` alone are **not** accepted by API
validation (kubernetes/kubernetes PR #117812 to relax this was closed unmerged). So:
- **cluster (default):** the **rclone sidecar** container is `privileged` and does the mount; the
  **anki container stays fully unprivileged** (drop `ALL`, no privesc, no added caps). The
  `anki-instances` namespace is therefore PSA-`privileged`, but it holds only Anki pods and the
  default-deny CiliumNetworkPolicy walls them in. The sidecar still runs as the non-root anki uid.
- **`external`/CSI (kept possible):** if a CSI driver presents the mount, the pod is a single
  baseline-clean container. Stock `csi-rclone` was rejected (node-local cache breaks §4 write-back
  durability; shared-plugin blast radius — ARCHITECTURE §12 item 9), which is *why* the sidecar won.

The image is agnostic: `CONTAINER_ROLE` + `MEDIA_MOUNT_MODE` select the shape (§5.3); the operator
renders the sidecar via `--media-mount-mode=sidecar` (default).

---

## 9. Sync model in the image (ARCHITECTURE §5, decision 14)

- **Native auto-sync stays ON** — profile-open and profile-close sync run as in stock desktop
  Anki. No config flips vs stock (this is why §3.4 sets `disable_native_auto_sync: false` and
  `strip_sync_link: false`, **overriding** ci-buddy's own hosted-default recommendation).
- The MCP `sync()` tool coexists (single sync *executor* = the AnkiMCP add-on;
  [../REQUIREMENTS.md](../REQUIREMENTS.md) §B.6). ci-buddy never triggers sync itself.
- **Sync credentials** arrive as the **hkey** via the mounted Secret at
  `${CI_BUDDY_CREDENTIALS_PATH}`; ci-buddy injects it into the profile
  ([../REQUIREMENTS.md](../REQUIREMENTS.md) §B). The **password never enters the pod**
  (ARCHITECTURE §6). The image's only responsibility here is to **mount the Secret as a whole
  volume** (never `subPath` — subPath never refreshes) with mode/owner readable by the Anki uid.
  Reference only; delivery is the operator's job.
- **B2 is not sync** — it is the pod-device's persistent local disk (ARCHITECTURE §5, decision
  13). Cross-device truth is AnkiWeb/the sync server.

---

## 10. Local dev story (docker-compose, `internal` variant)

A compose stack that runs the **full pod shape** locally (`MEDIA_MOUNT_MODE=internal`), against
either a real B2 test bucket **or** a local S3 for offline CI.

- **Offline / CI backend:** run `rclone serve s3` over a local directory as a stand-in "B2", so
  CI needs no B2 credentials. The Anki image's internal `rclone mount` points at that S3 endpoint.

```yaml
# docker-compose.hosted.yaml  (sketch — internal mount variant)
services:
  s3:                                   # local object store for CI (stands in for B2)
    image: rclone/rclone
    command: serve s3 /data --addr :8080 --auth-key testkey,testsecret
    volumes: [ "./_s3data:/data" ]

  anki:
    image: headless-anki:hosted-vX.Y.Z
    depends_on: [ s3 ]
    cap_add: [ SYS_ADMIN ]              # FUSE (internal mount) — local dev only
    devices: [ "/dev/fuse" ]
    security_opt: [ "apparmor:unconfined" ]
    environment:
      MEDIA_MOUNT_MODE: internal
      RCLONE_CONFIG_B2_TYPE: s3
      RCLONE_CONFIG_B2_PROVIDER: Other
      RCLONE_CONFIG_B2_ACCESS_KEY_ID: testkey
      RCLONE_CONFIG_B2_SECRET_ACCESS_KEY: testsecret
      RCLONE_CONFIG_B2_ENDPOINT: http://s3:8080
      B2_BUCKET: testbucket
      B2_PREFIX: alice
      ANKI_PROFILE: "User 1"
      CI_BUDDY_CREDENTIALS_PATH: /run/ankimcp/sync-credentials.json
    volumes:
      - "./_pvc:/data"                             # stands in for the per-user PVC
      - "./sync-credentials.json:/run/ankimcp/sync-credentials.json:ro"
    ports:
      - "5900:5900"    # VNC
      - "6080:6080"    # noVNC (open http://localhost:6080/vnc.html)
      - "3141:3141"    # MCP add-on
```

- Against a **real B2 test bucket:** swap the `s3` service out and point `RCLONE_CONFIG_B2_*` at
  B2 (native `b2` or S3 endpoint). This is the bring-up path for the "spot-check on a real
  bucket" residuals (§7.3, ARCHITECTURE §12 items 1 & 7).
- The compose stack **is** the `internal`-variant reference deployment; the cluster uses
  `external`.

---

## 11. Testing & CI

### 11.1 Image build CI

Extend headless-anki's `.github/workflows/docker.yaml`: build `hosted` after `x11-vnc` (it takes
`X11_TAG` as an ARG, like `x11-vnc-addons`), multi-platform (amd64 + arm64), push
`ghcr.io/ankimcp/headless-anki:hosted-{tag}`. Bake `ci_buddy` from this repo and the AnkiMCP
add-on from its pinned source/ID.

### 11.2 E2E smoke (container starts → everything live)

A single E2E asserting the whole pod shape, using the `internal` compose stack (§10) with the
local-S3 backend:

1. **Container starts** and reaches Ready (readiness probe green, §6.4).
2. **VNC reachable:** TCP connect to `5900` (localhost inside the pod).
3. **noVNC WS reachable:** WebSocket handshake on `6080` (`/websockify`) succeeds; `vnc.html`
   serves 200.
4. **Add-on loaded:** MCP port `3141` accepts and answers a trivial MCP call (proves the
   collection is open and the AnkiMCP add-on booted).
5. **Media mount live:** through Anki (MCP `store_media_file`), write a media file; assert it
   lands in the backing bucket (`_s3data/…/collection.media/`), and that a delete moves it into
   `media.trash/` **without `EXDEV`** (proves the same-mount symlink invariant §4.2).
6. **ci-buddy lock (REQUIREMENTS §8 E2E-over-VNC hooks):** via the MCP add-on's ability to run in
   the Anki process (or a VNC-driven check), assert
   `mw.form.actionPreferences.isVisible()/.isEnabled() == False` (same for Add-ons, Switch
   Profile), that `dialogs.open("Preferences"|"AddonsDialog")` are disabled, that
   `dialogs.open("AddCards"|"Browser")` are **enabled** (over-broad-filter regression guard), and
   that `writeConfig` is neutered. These are the exact assertions
   [../REQUIREMENTS.md](../REQUIREMENTS.md) §8 defers to "an E2E test in headless-anki over VNC" —
   this image is where they run.

### 11.3 preStop / drain test

Kick a media write, then invoke the `preStop` script; assert Anki quits cleanly, the rclone drain
predicate (§7.3) reaches zero, and the file is present in the bucket before exit. Separately,
`SIGKILL` mid-write and assert the next start replays the cache and completes the upload
(cache-on-PVC durability, §7.5).

---

## 12. Config surface (env vars — summary)

| Env var | Default | Purpose |
|---|---|---|
| `ANKI_BASE` | `/data` | Anki base dir (PVC mount) |
| `ANKI_PROFILE` | `User 1` | Profile folder name |
| `MEDIA_MOUNT` | `/media/b2` | Media mount root (rclone or CSI) |
| `CONTAINER_ROLE` | `anki` | `anki` (full boot) or `rclone-sidecar` (mount-only; §5.3) |
| `MEDIA_MOUNT_MODE` | `internal` | `internal` (rclone-in-this-container) or `external` (mount provided by the sidecar/CSI) |
| `MEDIA_MOUNT_TIMEOUT` | `120s` | wait-for-media gate bound (§6.3) |
| `RCLONE_REMOTE` | `b2` | rclone remote name (matches `RCLONE_CONFIG_<NAME>_*`) |
| `B2_BUCKET` / `B2_PREFIX` | — | shared bucket + per-user prefix |
| `RCLONE_CACHE_DIR` | `/data/.rclone-cache` | VFS cache on the PVC |
| `RCLONE_CACHE_MAX` | `6G` | VFS cache LRU size cap (default 6G on the 10GB PVC — contracts.md §6) |
| `RCLONE_WRITE_BACK` | `5s` | VFS write-back delay |
| `CI_BUDDY_CREDENTIALS_PATH` | `/run/ankimcp/sync-credentials.json` | hkey delivery file |
| `ANKI_QUIT_TIMEOUT` | `60s` | preStop clean-quit bound (§7.1) |
| `RCLONE_DRAIN_TIMEOUT` | `45s` | preStop drain bound (§7.3) |
| `RCLONE_RC_ADDR` | `127.0.0.1:5572` | rclone rc (localhost only) |

Ports: **5900** VNC (localhost-bound, x11vnc), **6080** noVNC/WS (browser entry — the operator
names this container port `vnc-ws`), **3141** MCP add-on (~~forward-readiness~~ 2026-07-11:
readiness-probe target only — **not exposed via the per-user Service in v1**; MCP rides the
add-on's outbound tunnel connection, [contracts.md §7](./contracts.md)), **5572** rclone rc
(localhost only). Port names `mcp`/`vnc-ws` and the Service that fronts them are
[contracts.md §7](./contracts.md). AnkiConnect (8765) is **not** exposed — no AnkiConnect in v1
(ARCHITECTURE §9, decision 12).

---

## 13. Acceptance checklist

- [ ] `hosted` variant extends `x11-vnc`, tagged `hosted-vX.Y.Z`, built in CI (multi-arch).
- [ ] Anki `aqt==26.5` / py3.12; `ci_buddy` + AnkiMCP add-ons baked into an **image stash**
      (`/opt/ankimcp/addons/`), **not** onto the PVC mount path.
- [ ] **Managed add-on overlay** reconciles code + `config.json` onto `/data/addons21` on start,
      version-gated, **preserving `user_files/`**; upgrades land on image bump.
- [ ] ci-buddy hosted `config.json`: `provisioning_enabled=true`,
      `disable_native_auto_sync=false`, `strip_sync_link=false` (per decision 14 — **native
      sync ON**), `credentials_path` set.
- [ ] Paths match ARCHITECTURE §4: SQLite + addons21 + rclone cache on the **PVC**;
      `collection.media/` + `media.trash/` symlinked into **one** rclone mount; **no SQLite on
      FUSE**.
- [ ] rclone mount uses `--vfs-cache-mode full`, **`--use-server-modtime`**, long
      `--dir-cache-time`, `--cache-dir` on the PVC, LRU size cap; B2 remote from env/Secret with a
      per-user prefix-scoped key.
- [ ] `MEDIA_MOUNT_MODE` switch: image works both **internal** (rclone-in-image) and **external**
      (mount provided by the sidecar/CSI); external keeps the anki container `baseline`-clean.
- [ ] `CONTAINER_ROLE=rclone-sidecar` short-circuits the entrypoint to run **only** `rclone-mount.sh`
      (no X/VNC/Anki); the mount uses `--allow-other` with `user_allow_other` in `/etc/fuse.conf` so
      the anki container reads it across the propagation boundary; `prestop.sh` is role-aware
      (only the mount owner unmounts; the anki container drains via the sidecar's rc).
- [ ] Startup: profile bootstrap (non-interactive, seeded `prefs21.db`), **wait-for-media gate**
      (fail-closed), ordered launch (mount → symlinks → overlay → Anki → readiness); **no bogus
      startup `sqlite3 PRAGMA` step** (documented as Anki-owned).
- [ ] Readiness = Anki up **and** port 3141 accepting.
- [ ] `preStop`: clean Anki quit (close-sync runs) → bounded wait → rclone drain via
      `rclone rc vfs/stats` (`uploadsInProgress`+`uploadsQueued`→0) → unmount → exit;
      `terminationGracePeriodSeconds: 120` recommended; SIGKILL path documented safe.
- [ ] Security: whole stack runs **non-root**; `automountServiceAccountToken:false`; no k8s creds
      in the Anki container; resource limits defaulted; FUSE privilege **decided (2026-07-10):
      privileged rclone sidecar** (§8.4), with both variants spec'd.
- [ ] Local dev docker-compose runs the full pod shape (internal variant) against local
      `rclone serve s3` (CI) or a real B2 test bucket.
- [ ] E2E smoke: start → VNC(5900) → noVNC WS(6080) → add-on(3141) → media write lands in bucket,
      delete → `media.trash` (no `EXDEV`) → ci-buddy lock assertions (REQUIREMENTS §8 hooks).

---

## 14. Open questions

1. **Clean-quit trigger (§7.2).** Genuinely open; collected in
   [contracts.md → Cross-cutting open decisions #3](./contracts.md). Does headless `aqt` run its
   close-time auto-sync on a bare `SIGTERM`? If not, `preStop` must invoke an **AnkiMCP-add-on
   shutdown action** that calls `mw.close()` on the Qt main thread. Do not assume `SIGTERM` is
   graceful. (Cross-repo: may need a small `shutdown` action in `anki-mcp-server-addon`.)
2. **rclone backend for B2 (§5.2).** Collected in
   [contracts.md → Cross-cutting open decisions #5](./contracts.md). Native `b2` backend vs
   `s3`-at-B2-endpoint — pick the one that matches the key type `media-library-b2-provisioning.md`
   already provisions. Affects `RCLONE_CONFIG_*` shape and the egress FQDN.
3. ~~**SQLite pragma reality (§6.2).**~~ **RESOLVED (2026-07-10):** ARCHITECTURE §4 is corrected to
   agree — WAL is Anki's persisted default and provides the crash-safety property; the platform
   ships **no startup pragma step**. Setting `synchronous`/`wal_autocheckpoint` via an Anki-side
   hook remains an **optional** future tuning item, not a v1 requirement.
4. **External-mode drain (§5.3 / §7.4).** For the **sidecar** shape this is **RESOLVED**: the rc is
   in-pod (the sidecar's), reachable from the anki container over localhost, and native-sidecar
   ordering keeps it alive during the drain (§7). Still open only for a **true CSI external**
   deployment, where the rc belongs to the CSI DaemonSet — define how the drain is coordinated there
   (its own `rc`, a drain API, or reliance on cache-on-PVC + resume). Ties to ARCHITECTURE §12 item 9.
5. **Readiness health path (§6.4).** Does the AnkiMCP add-on expose an HTTP health endpoint on
   3141, or only the raw MCP transport? If only raw MCP, readiness uses a TCP-connect probe; an
   explicit health path would be more precise.
6. **Check Media locking (ARCHITECTURE §12 item 7).** Decide whether hosted ci-buddy config also
   locks **Check Media** — it re-reads/re-hashes files in some paths, which is costly over B2.
   Softened by `--use-server-modtime` but not eliminated; measure on a real bucket during
   bring-up.
7. **Drain predicate on real B2 (§7.3, ARCHITECTURE §12 items 1 & 2).** `vfs/stats` fields are
   confirmed in rclone docs; spot-check the exact zero-predicate and the HEAD-storm cost model on
   a real B2 bucket during bring-up.
8. **noVNC pin & CSP.** Pin noVNC/websockify versions and confirm the self-hosted assets satisfy
   the dashboard's embedding (ARCHITECTURE §9 embeds noVNC); no CDN references (self-contained).
