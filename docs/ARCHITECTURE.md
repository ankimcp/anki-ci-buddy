# Anki-as-a-Service — Platform Architecture

**Status:** decisions closed 2026-07-09. This is the map; per-component requirements live in
sibling `docs/requirements-*.md` files (see §14). The ci-buddy add-on itself is spec'd in
[../REQUIREMENTS.md](../REQUIREMENTS.md) — two of its sections are superseded by this doc (§13).

Every major decision here was backed by a dedicated investigation (Anki source, k8s/rclone/KEDA
docs); the decision log (§10) records what was rejected and why, so decisions aren't re-litigated
by accident.

---

## 1. Product model

Cloud-hosted per-user Anki: each user gets their **own pod** running desktop Anki (locked down by
the ci-buddy add-on), reachable via MCP and a browser-embedded VNC view.

- **Pod runs while the user is active.** Activity = a connection attempt on any transport
  (MCP/HTTP call, VNC websocket connect).
- **Idle TTL (~1h) → suspend:** the pod is scaled to zero; the user's disk (PVC) is retained.
- **Request during suspend → delayed, not rejected:** the request is held while the pod scales
  0→1, then forwarded. The user sees a slow response (~seconds), never an error.
- The user's Anki behaves like a **regular desktop install** w.r.t. sync (see §5).
- Public reachability: via the cluster's existing **Cloudflare Tunnel** ingress, which routes
  directly to Services — ~~the activator is the Service it routes Anki traffic to~~
  **(2026-07-11, §16: MCP enters via the SaaS tunnel service's existing public `/mcp`; browser
  VNC enters via a same-origin `/vnc` route to the VNC gateway)**.
- **(2026-07-11, §16)** The "request during suspend → delayed" promise now applies to **MCP only**
  (the tunnel holds the request while the lifecycle service wakes the pod); the **VNC path has no
  wake** — the user starts a sleeping pod manually from the dashboard.

## 2. Component map

Two planes. They do not chain into each other — two doors into the same house.

```
── control plane (manage what exists) ──────────────────────────────────────────
web dashboard ──> lifecycle service ──> AnkiInstance CR ──> operator ──> StatefulSet
                  (Node/TS, in            (CRD, one per       (Go,         + PVC
                   anki-mcp-saas)          user)               reconciler)  + Service
                                                ▲                           + Secret mount
── data plane (live traffic) ───────────────────│───────────────────────────────
user traffic ────────> activator ───────────────┘──────────────────────> user's pod
(MCP / HTTP / WS-VNC)  (custom, Go, stateless)  patches replicas 0→1,
                                                holds request until Ready,
                                                forwards, tracks idle TTL,
                                                drains rclone, scales to 0
```

| Component | Repo | Role |
|---|---|---|
| **lifecycle service** | `anki-mcp-saas` (new atomic service, like tunnel / studio-media / knowledge-map) | Management front door: create/delete instance, on/off, reset, future config toggles. Only ever writes/patches `AnkiInstance` CRs. |
| **activator** | new (Go) | Traffic front door: wake + hold + forward + idle-tracking. See §7. |
| **operator** | new (Go, kubebuilder-style) | Reconciles each `AnkiInstance` CR into StatefulSet + PVC + Service + Secret mount. See §8. |
| **pod** | image from `headless-anki` | Anki + ci-buddy add-on + AnkiMCP add-on + rclone mount + VNC server + websockify/noVNC. |
| **dashboard GUI** | `anki-mcp-saas` | On/off, reset, embedded noVNC page. See §9. |
| **ci-buddy add-on** | this repo | GUI lock + sync-credential injection ([../REQUIREMENTS.md](../REQUIREMENTS.md)). |

> **2026-07-11 revision (see §16):** v1 ships **without the activator** — the data-plane half of
> the map above is superseded. MCP reaches hosted pods through the existing SaaS **tunnel**
> service (the pod's AnkiMCP add-on dials **out** to it); the VNC door is a new small
> **VNC gateway** (dumb authenticated pipe, `anki-mcp-saas` stack); wake + idle TTL move to the
> **lifecycle service**, which becomes the **single writer** of the CR spec (contracts.md §2).

## 3. The per-user runtime unit

One **StatefulSet per user**, `replicas` flipped `0↔1` for suspend/resume:

- `volumeClaimTemplate` → a stable per-user PVC that re-binds on every resume.
- **`persistentVolumeClaimRetentionPolicy: { whenDeleted: Retain, whenScaled: Retain }`** —
  `whenScaled: Retain` is load-bearing: `Delete` would wipe the user's disk on every idle cycle.
  (It's the default; set it explicitly anyway.)
- PVC access mode **RWOP** (`ReadWriteOncePod`) — structurally prevents two pods from opening the
  same SQLite WAL DB (a lingering old pod + new pod would corrupt it).
- **Storage class: Hetzner Cloud Volumes via `hcloud-csi`** — network block disks, decoupled from
  node-local disk (Longhorn-on-local-disk capped us at ~20 users on 3×180GB nodes).
  ⚠️ **Greenfield on this cluster** (verified 2026-07-09): today the only StorageClass is Longhorn
  1.11.2 (3× replicas on local NVMe, snapshotter disabled); `hcloud-csi` is **not installed** and
  `hcloud-CCM` is deliberately off — adding both is a prerequisite infra task.
- **Node pinning from day 0:** Cloud Volumes cannot attach to Hetzner dedicated servers. Label
  nodes (`cloud` / `dedicated`) and pin stateful Anki pods to Cloud nodes now, before dedicated
  workers are ever added. Dedicated nodes later run stateless components.
- Pod containers: Anki (customer-facing: MCP addon + VNC; **unprivileged, no k8s creds**) and the
  rclone mount (privilege model — sidecar container vs CSI — decided at image design time).

## 4. Storage design

A user's data splits into two stores by one rule: **SQLite on block storage, media blobs on B2.**

```
profile/                            ← per-user PVC (Hetzner Cloud Volume, 10GB — min volume size)
├── collection.anki2  (+wal/shm)    ← SQLite            → PVC  (never FUSE)
├── collection.media.db2 (+wal/shm) ← SQLite, mmaps -shm → PVC  (never FUSE)
├── prefs21.db                      ← SQLite, holds hkey → PVC  (never FUSE)
├── addons21/                       ← add-ons, meta.json, user_files → PVC
├── collection.media/  ──symlink──┐
└── media.trash/       ──symlink──┤→ ONE rclone mount → Backblaze B2 (locked choice)
                                  └─ rclone vfs cache dir → PVC (LRU-capped, default 6GB cap)
```

**Why:** users reach ~30GB media — too big to copy on every resume, too expensive to keep on
replicated block storage. B2 + on-demand fetch solves both. SQLite must never sit on an
object-store FUSE mount (WAL locking + `-shm` mmap break).

**rclone (`rclone mount --vfs-cache-mode full`):**
- Maps FS ops → B2 on demand; local LRU cache serves hot files (reads: no repeat egress;
  writes: async write-back upload).
- **Cache dir lives on the PVC** — write-back is async, so a crash before upload-flush must leave
  the pending file on durable disk; rclone finishes the upload on resume.
- **`collection.media/` and `media.trash/` must be on the same mount** — Anki deletes media via
  `fs::rename` into the *sibling* `media.trash/`; a mount boundary between them throws `EXDEV`.
- Cache LRU cap: **default 6GB on the 10GB PVC** (leaves headroom for DBs + WAL + addons) —
  never the full media set.
- **Required flags: `--use-server-modtime` + long `--dir-cache-time`.** Verified empirically
  (see §11 item 1): Anki rescans the media folder on the first sync after a write and after
  every pod restart (rclone's synthesized dir-mtime defeats Anki's skip fast-path — harmless
  for correctness, Anki's media DB is authoritative for its own writes; see §12 item 1). Without
  `--use-server-modtime`, a cold rescan issues **one HEAD per file** (~30k requests for a 30GB
  library); with it, modtimes ride the LIST (~30 requests) and stay consistent, so no re-hashing.

**Verified against Anki source (25.09.2):** media writes are whole-file (`std::fs::write`), no
partial/append writes, no fsync/flock/chmod/symlink/mmap on media files — the access pattern
object storage handles natively. The rejected alternatives (JuiceFS, Mountpoint-S3) and the one
open verification item (directory-mtime semantics over rclone — Anki's change-tracker keys on
folder mtime) are in §11/§12.

**SQLite crash-safety:** a pod OOM/SIGKILL is an *application* crash (node kernel survives), so
WAL auto-replays on next open — no corruption, no committed-data loss. **(Corrected 2026-07-10:)**
`journal_mode=WAL` is **Anki's own default and persists in the DB header**, so WAL alone provides
this crash-safety property. `synchronous`/`wal_autocheckpoint` are **per-connection** settings Anki
controls when it opens the DB, so the platform ships **no startup pragma step** — a startup
`PRAGMA` would be overwritten the moment Anki opens the DB and is therefore a no-op (see
requirements-headless-anki-image §6.2). `preStop` checkpoint is an optimization, not correctness.

**Backups:** back up **only the SQLite DB** — logical (`sqlite3 .backup` / `VACUUM INTO`) to B2.
Never snapshot the whole PVC: the rclone cache is reconstructible from B2; snapshotting it is
wasted spend. (~$3.5/mo per 1000 users at B2 $6.95/TB — verified 2026-07.)

**Verified prices (2026-07, official sources; Hetzner raised prices 2026-06-15):** Hetzner Cloud
Volume **€0.0572/GB/mo, minimum 10GB** — so the per-user PVC is a 10GB volume (≈ €0.57/user/mo),
which comfortably fits DB + a larger rclone cache; max 16 volumes per server (an attach ceiling
to watch at scale). B2: $6.95/TB/mo storage, egress free up to 3× stored then $0.01/GB,
transaction classes A/B/C free → 30GB media ≈ $0.21/user/mo. Warm-pod RAM context: CX43
€15.99/mo ⇒ ~€1.1/usable GB — a 512MB warm pod costs about as much as its PVC; scale-to-zero is
what keeps unit economics sane.

## 5. Sync model

**The pod is just another Anki desktop device.** (Decision 13+14.)

- **Cross-device source of truth:** AnkiWeb / the sync server — same as for any Anki user. The
  pod, the phone, the laptop are all peers syncing against it.
- **B2's role is NOT sync** — it is the pod-device's persistent local disk. Without it every
  resume = a fresh device re-downloading up to 30GB; with it, sync moves diffs only.
- **Native auto-sync stays ON** (profile open/close), sync button kept — stock behavior. The MCP
  `sync()` tool coexists. ci-buddy's ship defaults = hosted defaults (no flipped config).
- **Durability never depends on sync.** Sync failure = *staleness* (other devices lag), never
  *loss* (PVC holds everything). Failed sync + TTL downscale → data waits on the PVC; the user
  resolves on next wake. A both-sides-edited conflict surfaces Anki's stock conflict dialog over
  VNC — platform-independent Anki behavior.
- Accepted trade-off (decided with eyes open): the GUI sync path can pop conflict/auth dialogs
  that stall MCP jobs until dismissed over VNC.

**Graceful downscale sequence** (~~activator~~ **lifecycle**-triggered on TTL — 2026-07-11, §16):
quit Anki cleanly (close-time auto-sync runs) → drain rclone (poll pending uploads to zero via
`rclone rc`) → set replicas 0. Setting replicas 0 is the whole trigger — the pod's `preStop`
hooks own the quit/drain sequence. Ungraceful death needs no sequence: PVC + WAL + cache-on-PVC
cover it.

## 6. Sync credentials (hkey)

- Control plane (dashboard) holds the AnkiWeb password, mints the **hkey**; the password never
  enters the pod. Delivery: **plain k8s Secret mounted as a file** (whole-volume mount, never
  `subPath` — subPath mounts never refresh).
- **No credential sidecar** (supersedes ci-buddy REQUIREMENTS §C.2). The sidecar solved fast
  refresh for long-running pods; with scale-to-zero + kubelet's ~60s Secret refresh +
  ci-buddy's 1–2s file poll, rotation lands live in ≤ ~1 min with **no pod restart** — and the
  pod carries **zero k8s API credentials**. **(2026-07-11 — superseded for hosted v1, §16.6:**
  the ci-buddy live file-poll pickup path stays **built but UNUSED** for hosted v1 — hosted v1
  credential pickup = **pod restart via `spec.restartedAt`** (the lifecycle service bumps it
  after the Secret write when the pod is running; a fresh pod mounts the current Secret at
  start). The kubelet-refresh + file-poll mechanics above remain true and remain the
  fallback/upgrade path.**)**
- **Rotation UX ("Option 1"):** dashboard says "credentials active within a minute" after
  re-login; the MCP sync `auth_failed` payload carries a retry hint (~60s). The consumer is an
  LLM — a structured "retry after 60s" is obeyed automatically, making the window invisible.
  Upgrade path if needed: push delivery over an authenticated channel (file contract unchanged,
  only the writer changes). **Investigated 2026-07-10: do NOT reuse the saas tunnel for this** —
  it's opt-in, keyed on SaaS OAuth identity (a second credential the pod must not hold, §10
  layer 2), off the in-cluster traffic path, and has no fire-and-forget push message type today.
  If sub-minute push is ever demanded, add a tiny authenticated in-pod HTTP endpoint the
  lifecycle service POSTs to (writes the same credentials file the poller reads).

## 7. Activator (custom, data plane)

> **SHELVED FOR v1 (2026-07-11, user — see §16):** the activator is **not deployed** in v1. Its
> jobs are redistributed: MCP door → the SaaS **tunnel**; wake + request-hold → tunnel (hold) +
> **lifecycle service** (wake); idle TTL + downscale trigger → **lifecycle service**; VNC door →
> the new **VNC gateway**. The built Go component stays on the shelf (retire-vs-revive is
> POSTPONED, not decided); this section and `requirements-activator.md` are retained for
> possible revival.

One custom component — **not** KEDA HTTP Add-on, **not** Knative (§10, decision 6).

- **Stateless, no database.** "Who's awake" = asked from Kubernetes (source of truth);
  user→StatefulSet routing = naming convention, not stored data; last-activity timestamps =
  in-memory (worst case after a restart: an idle user gets a fresh hour — harmless).
- **v1 transports: HTTP + MCP + websockets only.** Browser noVNC arrives as a websocket, so no
  raw-TCP path is needed in v1; raw TCP is deferred to a possible native-VNC-client feature.
- Behavior per request: resolve user (from session/auth) → if replicas 0, patch the
  `AnkiInstance` CR to 1 → **hold** the connection until pod Ready → forward → record activity.
- Owns the **idle TTL** and the **downscale trigger** (it's the only component that sees live
  traffic), executing the graceful sequence in §5. The operator stays a dumb reconciler.
- If multiple activator replicas are ever needed: consistent per-user routing or a small shared
  timer store (Redis) — explicitly out of scope for v1.

## 8. Operator + `AnkiInstance` CRD

One CR per user is the single declarative unit; everyone else only expresses intent on it:

```yaml
kind: AnkiInstance                                    # anki.ankimcp.ai/v1alpha1 (see contracts.md)
metadata: { name: 3f9b2c1a-7d4e-4b21-9c8a-1e5f6a2b3c4d }  # = keycloakId (DNS-1123-safe UUID)
spec:
  user: 3f9b2c1a-7d4e-4b21-9c8a-1e5f6a2b3c4d          # same keycloakId; children are anki-<keycloakId>
  replicas: 0            # activator patches this to wake (2026-07-11: lifecycle-written in v1, §16.4)
  suspended: false       # lifecycle-owned power gate; effectiveReplicas = suspended ? 0 : replicas
  # future: ankiConnect: false, per-addon config, image/version pin
```

- Operator (Go, kubebuilder-style) reconciles CR → StatefulSet + PVC + Service + Secret mount.
- **Deciding argument for CRD-over-direct-API:** the fleet is long-lived and drifting — 0↔1
  flips, config toggles, and above all **image upgrades rolled out across thousands of per-user
  StatefulSets**. That's ongoing reconciliation, the exact workload the operator pattern exists
  for. Also self-healing (drift gets re-fixed) and fits the argocd/GitOps estate.
- Lifecycle service writes/patches CRs only; ~~activator patches `spec.replicas` only~~
  **(2026-07-11, §16.4: single-writer v1 — lifecycle writes all of spec, incl. `replicas` and the
  new `restartedAt`/data-fate fields; contracts.md §2)**.

## 9. Dashboard GUI — v1 scope

- **Controls:** instance on/off, reset, embedded **noVNC** page (VNC in the browser, no client
  install; requires adding websockify/noVNC to the headless-anki image, which today ships plain
  VNC only).
- **Auth/routing: one door, identity routes.** User logs into the dashboard (existing
  username+password); ~~the VNC websocket carries the session; the activator resolves user→pod
  from the session~~ **(superseded 2026-07-11, §16 / contracts.md open decision #1: the VNC
  websocket carries a short-lived single-use signed ticket minted by `apps/api`; the VNC gateway
  verifies it locally and resolves user→pod by naming convention; same-origin `/vnc`)**. No
  per-user URLs, no dedicated VNC domain, no second *user-held* credential (the ticket is derived
  from the session, not typed by the user).
- **No AnkiConnect in v1** — only the AnkiMCP add-on. If users ask: the API-key auth surface and
  the CR toggle are the prepared landing spots.
- All management controls follow one pattern: **patch desired state → reconcile/restart** —
  never live surgery on a running pod.

## 10. Security & tenant isolation

Threat model: the pod runs a desktop app exposed to its own user (VNC + MCP) — treat every pod
as **potentially attacker-controlled** and bound the blast radius to that user's own data.

**Cluster facts** (verified against `anki-mcp-infrastructure`, 2026-07-09): CNI = **Cilium
1.19.4** (eBPF, kube-proxy replaced, Hubble available) — it *enforces* NetworkPolicy and
CiliumNetworkPolicy, **but zero policy objects exist today**: the cluster is a flat allow-all
network. PodSecurity Admission is enforced cluster-wide at **`baseline`** (audit/warn
`restricted`). Public ingress = Cloudflare Tunnel routing **directly to Services** (bypasses
Traefik). Secrets pipeline = Bitwarden → Vault → External-Secrets. All three nodes are
control-plane + worker (no separation). We own the entire isolation story — greenfield.

**Layer 1 — network (the big one): default-deny CiliumNetworkPolicy on the Anki namespace.**
- **Ingress:** only from the ~~activator~~ **VNC gateway** (2026-07-11, §16.1). Nothing else —
  not even other Anki pods.
- **Egress:** DNS + AnkiWeb sync + the B2 endpoint (`s3.eu-central-003.backblazeb2.com`)
  **+ the in-cluster tunnel service** (2026-07-11, §16.1 — the add-on's outbound WS).
  Cilium supports **FQDN-based egress rules** (`toFQDNs`) — use them rather than brittle IP lists.
- **Explicitly denied:** the k8s API, the Hetzner metadata endpoint (`169.254.169.254`), and the
  flat-network crown jewels that any pod can reach today: `pg-shared` (Postgres, `postgres` ns),
  **Vault** (`vault` ns — holds every secret in the cluster), NATS, Keycloak, the `o11y` stack,
  ArgoCD, Longhorn, Traefik, and the `ankimcp` app services.
- Hubble gives observability to verify the policy actually bites.

**Layer 2 — minimal secrets in the pod.** No ServiceAccount token
(`automountServiceAccountToken: false`); no AnkiWeb password (only the revocable hkey); and a
**per-user B2 application key restricted to that user's prefix** (B2 `namePrefix` keys). Prior
art already in production: `docs/media-library-b2-provisioning.md` in the infrastructure repo
uses exactly this bucket-scoped-key + per-user-prefix model — reuse it, don't invent a second
one. Result: full pod compromise yields the attacker their **own** media and collection, nothing
else.

**Layer 3 — pod hardening within PSA `baseline`.** runAsNonRoot, drop capabilities, no privilege
escalation, resource limits (noisy-neighbor control on 3 shared CX43 nodes).
✅ **DECIDED (user, 2026-07-10): per-user rclone SIDECAR container in the pod.** (The CSI tilt
died on investigation — §12 item 9: stock rclone CSI drivers put the VFS cache node-local,
breaking §4's write-back durability, and a node-plugin restart/OOM kills every user's mount on
that node.) The sidecar natively satisfies §4 (cache on the pod's PVC), mount-lifecycle ==
pod-lifecycle (no orphan states), and per-user blast radius. Accepted cost: the `anki-instances`
namespace is labeled PSA-**`privileged`** — compensated by: the namespace holds ONLY Anki pods
(hygiene: relaxed admission and hostile-by-assumption pods are the same set), the sidecar gets
exactly `/dev/fuse` + `SYS_ADMIN` and drops everything else, the **Anki container itself stays
unprivileged** (per-container securityContext), and the default-deny CiliumNetworkPolicy walls
the pod in. Pod shape: anki container (`MEDIA_MOUNT_MODE=external`) + rclone sidecar mounting
into a shared volume with Bidirectional mount propagation; preStop drain reaches rclone's rc
over the pod-shared localhost. Rejected: custom DaemonSet (shared blast radius, CSI-grade
remount complexity we'd own forever).

**Layer 4 — the honest limit.** Containers share the node kernel; a kernel exploit escapes all
of the above. Aggravating factor here: **every node is also a control-plane node**, so hostile
workloads currently share iron with etcd/API-server. Upgrade paths, in order: dedicated worker
nodes for Anki pods (planned anyway for Hetzner dedicated servers), then a sandboxed runtime
(gVisor/Kata — Talos has extensions) if/when warranted. v1 ships layers 1–3 and documents this
limit rather than pretending containers are a hard boundary.

## 11. Decision log

| # | Decision | Rejected alternatives — why |
|---|---|---|
| 1 | Media on **Backblaze B2** (locked) via **rclone mount, vfs-cache full** | **JuiceFS**: stores opaque 4MiB chunks, not files → breaks media interop; needs per-user metadata engine. **Mountpoint-S3**: no rename — Anki's delete = `fs::rename` to `media.trash/`, would fail every delete; B2 endpoint second-class. **s3fs/goofys**: unbounded/no cache. **Copy-on-suspend**: 30GB users make copy-back infeasible; copy-on-shutdown never runs on ungraceful death. |
| 2 | SQLite DBs on **local PVC**, never FUSE | Object-store FUSE breaks WAL locking + `-shm` mmap. |
| 3 | Suspend/resume = **StatefulSet 0↔1**, `whenScaled: Retain`, **RWOP** | Bare Pod + controller: reinvents STS identity/rebind. Deployment: multi-attach wedges on RWO. |
| 4 | **Hetzner Cloud Volumes (`hcloud-csi`)**, not node-local Longhorn | Local disk caps ~20 users; volumes decouple storage from nodes. Constraint: pin stateful pods to Cloud nodes (dedicated servers can't mount Cloud Volumes). |
| 5 | Backup = **DB-only, logical, to B2** | Whole-PVC snapshots pay to store a reconstructible cache. |
| 6 | **Custom activator** | **KEDA HTTP Add-on**: does request-hold well and STS scale-to-zero works, but one static routing object per user (thousands of ScaledObjects/HPAs, no dynamic fan-out) and raw-TCP VNC bypasses its HTTP interceptor entirely → would force a second custom path anyway. **Knative**: stateless-Revision model fights per-user pod+PVC identity. KEDA-now/swap-later was viable; user chose custom directly. |
| 7 | **Activator owns idle TTL + downscale** | Only the traffic path knows last-activity. Control plane dumb, data plane smart. |
| 8 | **Drain rclone before scale-down** | TTL (1h ≫ seconds of write-back) + drain + cache-on-PVC = belt and suspenders. |
| 9 | **No credential sidecar** — plain Secret mount | Sidecar assumed long-running pods; scale-to-zero + live file-poll made it redundant; removing it removes k8s creds from the pod. Supersedes ci-buddy §C.2. |
| 10 | hkey rotation UX = **explicit + LLM-retryable** ("Option 1") | Push delivery (upgrade path), pickup-confirmation (plumbing for a status line), sidecar (see 9). |
| 11 | **`AnkiInstance` CRD + Go operator** | Direct-from-nodejs: no self-healing, no owner for fleet-wide image rollouts, k8s logic scattered into a web service. |
| 12 | GUI v1 = dashboard controls + **embedded noVNC**, session-routed, **no AnkiConnect** | Per-user URLs / second credentials: unnecessary — identity routes. AnkiConnect deferred until demand. |
| 13 | **Media-sync stays ON; B2 = device disk, not sync truth** | "B2 as store of record" would break other devices' view; the pod is a peer device. |
| 14 | **Sync trigger = full regular desktop** (auto-sync ON) | MCP-driven-only (the ci-buddy §B.6 model) was recommended to avoid GUI sync dialogs stalling MCP jobs; user chose stock behavior with that trade-off accepted. Supersedes §B.6 hosted-defaults. |
| 15 | Docs live in **this repo** (`anki-ci-buddy/docs/`) | Move out when component repos exist. |

## 12. Build-time verification list (open items — none block the architecture)

1. ~~Media folder mtime over rclone/B2~~ — **RESOLVED (2026-07-09, empirical).** No
   missed-changes risk: Anki records its own writes directly in `collection.media.db2`
   (`media/mod.rs:add_file`, `sync/media/syncer.rs:fetch_changes`); the dir-mtime check
   (`changetracker.rs`) is a skip-optimizer only. Failure mode = extra rescans (first sync
   after a write; every pod restart, since rclone reports a constant `--default-time`
   2000-01-01 for synthesized dirs on cold reads). Cost without mitigation: n+1 HEAD-per-file
   (~30k for 30GB). **Mitigation (required): `--use-server-modtime`** (+ long
   `--dir-cache-time`); mtime precision is a non-issue (Anki compares i64 seconds). Residual:
   the HEAD-storm cost model was inferred from rclone docs/forum, not reproduced on real B2 —
   spot-check on a real bucket during image bring-up.
2. ~~rclone `rc` drain command~~ — **RESOLVED (2026-07-10, empirical, rclone 1.74.4).** Gate the
   preStop on `POST /vfs/stats` → `diskCache.uploadsQueued + uploadsInProgress == 0` (also check
   `erroredFiles`). Force-flush (skip write-back timers): `POST /vfs/queue-set-expiry id=<n>
   expiry=-1` per queued item (needs rclone ≥1.68). Mount needs `--rc`. Confirmed: unmount/SIGTERM
   does NOT flush the queue (upstream #8767) → the preStop drain is mandatory; tested drain script
   in `requirements-headless-anki-image.md` §5.
3. ~~Prices~~ — **RESOLVED (2026-07, official sources):** Hetzner Volume €0.0572/GB/mo (min
   10GB, max 16 volumes/server — a per-node attach ceiling to watch at scale), CX43 €15.99/mo,
   CX53 €29.49/mo, B2 $6.95/TB/mo (A/B/C transactions free). Hetzner prices rose 2026-06-15;
   quotes excl. VAT + ~€0.60/mo per public IPv4.
4. The ~6-min multi-attach force-detach tail when a node dies ungracefully (resume-latency worst
   case) — verify value on our cluster; automate stale-`VolumeAttachment` cleanup if needed.
5. ~~Whether hosted pods have a saas-tunnel connection~~ — ~~**RESOLVED (2026-07-10, code
   inspection):** no — the tunnel is opt-in NAT traversal for *local* Anki (needs stored SaaS
   OAuth creds; nothing auto-connects it), redundant with the activator path in-cluster, and its
   protocol has no push/notify message type.~~ **SUPERSEDED (2026-07-11, user — §16): hosted pods
   DO connect to the tunnel.** The pivot inverts the 2026-07-10 conclusion: the tunnel is the MCP
   door for hosted instances (in-cluster internal URL; the add-on dials out with a
   lifecycle-minted token, contracts.md §8c — no stored SaaS OAuth creds needed), and with the
   activator shelved it is not redundant with anything. The 2026-07-10 note stands only for what
   it actually checked: the tunnel is still not a credential-push channel (decision-10 upgrade
   path in §6 unchanged — hosted v1 uses restart-based hkey pickup anyway, §16).
6. ~~FUSE privilege model in the pod (rclone sidecar container vs CSI approach).~~ **RESOLVED
   (user, 2026-07-10): per-user rclone SIDECAR** (§10 layer 3). Implemented in `operator/`
   (`--media-mount-mode=sidecar`, default) + `headless-anki-image/` (`CONTAINER_ROLE=rclone-sidecar`);
   pod shape in `docs/contracts.md §10`. The sidecar is `privileged` because k8s requires it for the
   Bidirectional mount propagation that carries the FUSE mount to the anki container (verified;
   kubernetes/kubernetes PR #117812 to allow `SYS_ADMIN`-only was closed unmerged) → namespace is
   PSA-`privileged`; the anki container stays unprivileged.
7. Consider locking **Check Media** in hosted ci-buddy config. Softened by item 1's results
   (`--use-server-modtime` makes the scan ~LIST-only), but Check Media additionally re-reads
   files to re-hash in some paths — decide with a real-bucket measurement during bring-up.
8. **Install `hcloud-csi` (+ likely `hcloud-CCM`)** — prerequisite for decision 4; today's only
   StorageClass is Longhorn. CCM was off simply because nothing needed it yet (owner-confirmed
   2026-07-09) — enabling it is uncontroversial; it adds a Hetzner API token to the cluster and
   a runtime dependency on the Hetzner API, note it in the infra repo when done.
9. ~~csi-rclone maturity~~ — **INVESTIGATED (2026-07-10), verdict NEGATIVE for stock CSI.**
   Per-volume config is fine (veloxpack/SDSC both support per-user Secrets + prefix templating),
   but two structural failures: VFS cache is node-local (can't sit on the user's PVC → §4
   write-back durability degrades to "seconds of just-written media lost on plugin death") and
   no driver isolates the FUSE daemon from the node plugin (restart/OOM = stale mounts for ALL
   users on the node; veloxpack issues #58/#74 open+reproduced; shared-plugin blast radius
   contradicts §10). **RESOLVED (user decision 2026-07-10): per-user rclone sidecar** —
   privileged namespace + compensating controls; see §10 layer 3 for the full accepted-cost
   rationale. The image's `MEDIA_MOUNT_MODE` switch made this non-blocking during the interim.
10. **Capacity planning:** 3× CX43 (48GB RAM total) is sized tightly around N-1 failover of the
    existing workload; model warm-pods-per-node before onboarding real users (the infra doc's
    scale trigger is CX53/4th node).
11. ArgoCD sync waves are currently non-functional in the cluster (all apps deploy at once,
    converge by retry) — the operator/CRD app must tolerate racing its dependencies; use
    `ServerSideApply=true`.

## 13. Cross-repo impact

| Repo | Impact |
|---|---|
| **this repo** | ci-buddy REQUIREMENTS **§C.2 (credential sidecar) superseded** by §6; **§B.6/Seam 5 hosted-defaults superseded** by §5 (no flipped sync config). Update on next spec touch. Component requirement docs to be added under `docs/`. |
| **headless-anki** | Image additions: rclone (+mount wiring, cache-on-PVC, symlinks), websockify/noVNC, bake ci-buddy + AnkiMCP add-ons, graceful-quit hook for the downscale sequence. |
| **anki-mcp-saas** | New atomic **lifecycle service**; dashboard pages (on/off, reset, noVNC embed, re-login flow with "active within a minute" note). **(2026-07-11, §16:)** plus the new **VNC gateway** (dumb authenticated pipe in the `anki-mcp-saas` stack, §16.3) and **tunnel-side work**: hosted-token validation against the lifecycle signing key (contracts.md §8c), the `ensureConnected` wake call + request hold (§16.5), and marking hosted connections as such. |
| **anki-mcp-server-addon** | `auth_failed` payload gains the retry-after hint; regression guard: sync must keep building `SyncAuth` fresh per run (ci-buddy §B.7). **(2026-07-11, §16.1:)** new **`hosted_mode`** — auto-connect to the tunnel on profile open using the mounted `tunnel-credentials.json` token (contracts.md §8c), static token, **no reconnect ever** (all disconnects terminal), no interactive auth. |
| **anki-mcp-helm-charts / infrastructure** | Install `hcloud-csi` (+ CCM decision) + storage class; node labels (cloud/dedicated) + pinning (none exist today); CiliumNetworkPolicy objects (first in the cluster); new argocd apps per the app-of-apps pattern (`argocd/apps/<name>/` + tier entry; CRDs in an early tier, operator mid-tier, per repo convention; charts published to Harbor OCI like the `ankimcp` umbrella); per-user B2 keys via the existing Bitwarden→Vault→ESO pipeline, reusing the `media-library-b2-provisioning.md` model. |

## 14. Repo layout & build plan

**This repo is the platform monorepo** (decided 2026-07-09): components are implemented here as
subfolders, then bound together. Implementation starts only on explicit go-ahead.

```
anki-ci-buddy/            ← platform monorepo (name may outgrow itself)
├── docs/                 ← this doc + per-component requirements
├── addon/                ← ci-buddy add-on (spec: ../REQUIREMENTS.md)
├── activator/            ← custom activator (Go)
├── operator/             ← AnkiInstance CRD + reconciler (Go)
└── ...                   ← further components as they materialize
```

Open detail: whether the **lifecycle service** also lands here or stays an atomic service inside
`anki-mcp-saas` (the earlier decision) — to be confirmed.

Build order (dependency-first): ~~**addon → headless-anki image → operator → activator →
lifecycle service → dashboard GUI.**~~ **(revised 2026-07-11 — the activator is struck from the
v1 order (shelved, see §16); the VNC gateway is added):** **addon → headless-anki image →
operator → lifecycle service → tunnel-side changes → VNC gateway → dashboard GUI.** The addon
goes first: zero infra dependencies, testable against a local Anki.

The **shared cross-component contract registry** — GVR/kind, CR field ownership, naming scheme,
namespace, labels, ports/DNS, the credentials Secret + B2 key/prefix scheme — lives in one place:
[`docs/contracts.md`](./contracts.md). Every per-component doc references it; where a doc restates
a contract it must match contracts.md.

Planned per-component requirement docs:

- `docs/requirements-activator.md`
- `docs/requirements-operator.md`
- `docs/requirements-lifecycle-service.md`
- `docs/requirements-headless-anki-image.md`
- `docs/requirements-dashboard-gui.md`

## 15. Licensing (AGPL analysis — not legal advice)

Anki is **AGPL-3.0**. Its §13 network clause: if you *modify* the program and users *interact
with it over a network* (our users do — VNC, MCP), you must offer those users the source of the
modified version. The boundary copyleft respects is the **process/linking boundary**, not the
product boundary:

| Component | Relation to Anki's process | Obligation |
|---|---|---|
| Anki + any image patches | — | offer corresponding source to service users |
| ci-buddy add-on | in-process (imports `aqt`) | derivative → **AGPL, source offered** (already declared AGPL) |
| AnkiMCP add-on | in-process | same — already AGPL |
| activator, operator, lifecycle service, dashboard, charts | separate programs, arm's-length (HTTP/VNC/k8s API) | independent works — **may stay closed** |
| VNC gateway (added 2026-07-11, §16.3) | separate program, arm's-length (WebSocket pipe) | independent work — **may stay closed** (same class as activator/lifecycle/dashboard) |

**Compliance mechanism:** a visible "source code" link for service users (dashboard footer /
VNC page) pointing to the corresponding source of the shipped Anki build + baked add-ons.

**Visibility model (DECIDED 2026-07-10):** **this repo stays PRIVATE (internal dev)**. The
**addon is published to its own dedicated public repo** (satisfies AGPL §13 — its source is
offered to service users via the dashboard/VNC "source code" link). The **activator, operator,
lifecycle service, and dashboard integrate into the closed-source SaaS** (`anki-mcp-saas`) —
arm's-length independent works, no source-offer obligation. So the private-repo-with-mixed-code
worry dissolves: the only thing that must be public is the addon, and it moves out to its own
repo. **headless-anki image**: contains upstream Anki (already public) + the addons (public) as
aggregation; the image *build recipe* (Dockerfile, VNC/rclone glue) is packaging, not an Anki
derivative, so it **may stay private** — the one caveat is that any *patch to Anki's own source*
would be AGPL-covered and must be offered (baking addons in is aggregation, not patching).
Lawyer for edges; table above follows FSF standard interpretation.

## 16. 2026-07-11 revision — the tunnel is the MCP door; activator shelved

**DECIDED 2026-07-11 (user).** This addendum supersedes: §2's data-plane map, §7's *deployment*
(not its design — the doc is kept for revival), §9's session-carried VNC auth, §12 item 5, and
contracts.md §2's two-writer field-ownership contract. Component-doc deltas are marked inline in
each `requirements-*.md`.

### 16.1 The pivot: hosted pods connect to the SaaS tunnel

Hosted pods **DO** connect to the SaaS **tunnel** service: the baked AnkiMCP add-on runs in
`hosted_mode` and opens an **outbound WebSocket** from the pod to the tunnel's **in-cluster
internal URL**, authenticated by a lifecycle-minted bearer token delivered as a second data key in
the credentials Secret (contracts.md §8c). The tunnel is the MCP door for hosted instances — the
**same endpoint LLM clients already use for local Anki**. Consequences:

- Pod inbound MCP port **3141 is NOT exposed** via the per-user Service in v1 — nothing dials it;
  MCP rides the add-on's outbound tunnel connection. `vnc-ws` 6080 stays (contracts.md §7).
- The "forward-readiness gate" concept on 3141 is **moot**; the readiness signal for MCP =
  "add-on connected to the tunnel".
- Namespace NetworkPolicy: **ingress only from the VNC gateway** (§16.3); **egress adds the
  in-cluster tunnel service** (DNS + AnkiWeb + B2 stay) — contracts.md §4.

### 16.2 Activator SHELVED for v1 (postponed, not deleted)

v1 ships **without deploying the activator**. The built Go component stays on the shelf in this
repo; formally retiring vs reviving it is **POSTPONED, not decided**. Its jobs are redistributed:

| Former activator job | v1 owner |
|---|---|
| MCP forwarding | **tunnel** (§16.1) |
| wake + request-hold | **tunnel** holds the MCP request; **lifecycle service** performs the wake (§16.5) |
| idle TTL + downscale trigger | **lifecycle service** — activity signals now exist off the traffic path: the tunnel already publishes per-request quota events, and the VNC gateway will signal VNC session activity. Setting `replicas: 0` is sufficient for graceful downscale — the pod's `preStop` hooks own the quit/drain sequence (contracts.md §10) |
| VNC door | new **VNC gateway** (§16.3) |

### 16.3 New component: VNC gateway

A deliberately **dumb authenticated pipe**: (1) verify a **short-lived, single-use, signed VNC
ticket** — minted by the SaaS `apps/api`, which owns browser sessions; the gateway verifies
**locally with a public key** ("option B", DECIDED 2026-07-11); (2) resolve the user's pod by the
naming convention (`anki-<keycloakId>-0.anki-<keycloakId>…:6080`, contracts.md §3); (3) pipe
WebSocket bytes. **NO wake logic** (wake is manual from the dashboard, §16.4), **no k8s API
access, no DB**. Lives in the `anki-mcp-saas` stack; exact placement (own app vs endpoint on an
existing service) is an implementation detail. **Same-origin `/vnc` path routing** via the
existing Cloudflare Tunnel ingress — no `vnc.` subdomain, so the cookie/`SameSite` question
dissolves (resolves contracts.md cross-cutting open decision #1).

### 16.4 CR single-writer + dashboard model

- **Single writer (v1):** the **lifecycle service is the ONLY writer** of the `AnkiInstance` spec
  (`user`, `suspended`, `replicas`, `restartedAt`, `image`, data-fate). contracts.md §2's
  two-writer table is superseded for v1 (revived if the activator returns). Operator unchanged:
  reconciles spec, writes status only.
- **Dashboard model (DECIDED):** pod status shown as three states — **off** (user power gate) /
  **sleep** (replicas 0) / **on** (running). VNC button disabled when off; from sleep the user
  wakes the pod **MANUALLY** (Start button = dashboard → api → lifecycle); VNC connects only when
  on. The VNC path therefore has **no wake logic at all**.

### 16.5 Tunnel connection rules (DECIDED)

1. **Last client connected wins** (existing tunnel kick behavior, unchanged). The hosted add-on
   **NEVER auto-reconnects** — all disconnects are terminal in hosted mode (kick, auth-reject,
   network drop alike) — so it can never fight a local client.
2. **MCP request arrives + no client connected:** the tunnel makes **ONE NATS call** to the
   lifecycle service ("ensure user X's instance is connected"). Lifecycle checks instance-exists
   + tier + power-gate and picks the lever: asleep → set `replicas: 1`; running-but-disconnected
   → bump `spec.restartedAt`. The tunnel **holds the request** until the add-on connects
   (~60–90s timeout), then forwards; on failed checks the tunnel rejects with a typed reason.
   AnkiWeb login is **NOT** required for MCP (it affects sync only). **No auto-create** of
   instances.
3. **One mechanism** covers the sleeping pod, the kicked pod, and the dashboard Reconnect button:
   (re)start pod → add-on connects on startup (~30–60s accepted for v1).

### 16.6 Restart mechanism (resolves lifecycle §15.2 / operator open item)

New CR field **`spec.restartedAt`** (RFC3339 timestamp, lifecycle-written). The operator copies
the value into the StatefulSet pod template as an **annotation** (the `kubectl rollout restart`
pattern) → pod recreated, **PVC untouched**. Used for: **hkey pickup after AnkiWeb (re)login**
(v1 uses restart, NOT ci-buddy live injection — the live-pickup path stays built but unused for
hosted v1) and **tunnel reconnect/takeover** (§16.5).

### 16.7 PVC deletion (resolves lifecycle §15.2b / §5.6 open item)

**Operator-owned.** A CR field expresses data fate on delete (e.g.
`spec.dataRetention: Delete|Retain` — the operator spec owns exact naming); the operator deletes
the PVC (**finalizer flow**) when the CR is deleted with fate=Delete. The lifecycle service keeps
**zero workload-object RBAC verbs** (its Role stays CRs + Secrets only).

### 16.8 Decision-log deltas (§11)

- **Decision 6 (custom activator):** the component is built but **shelved for v1** (§16.2).
- **Decision 7 (activator owns idle TTL):** the TTL moves to the **lifecycle service** — the
  "only the traffic path knows last-activity" premise no longer holds (tunnel quota events +
  VNC-gateway session signals are activity sources off the proxy path).
- **Decision 12 (session-routed VNC):** session-carried routing is replaced by the api-minted
  signed ticket + VNC gateway (§16.3).
