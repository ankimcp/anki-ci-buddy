# contracts — shared cross-component contract registry

**Status:** reconciled 2026-07-10; **revised 2026-07-11**. This is the single source of truth for
the contracts that span more than one component of the Anki-as-a-Service platform. Where any
`requirements-*.md` or [ARCHITECTURE.md](./ARCHITECTURE.md) restates one of these values, it
**must match this file**; a divergence is a bug in that doc, not here. Component docs keep their
own local context and rationale — this file fixes the *values* they share.

> **Revision 2026-07-11 (user — see [ARCHITECTURE §16](./ARCHITECTURE.md)):** hosted pods connect
> to the SaaS **tunnel** service — the tunnel is the MCP door for hosted instances. The
> **activator is SHELVED for v1** (its jobs move to the tunnel, the lifecycle service, and a new
> small **VNC gateway**); the CR spec becomes **single-writer (lifecycle)** and gains
> `spec.restartedAt` + a PVC data-fate field. Deltas are marked inline below (§2, §3, §4, §7,
> §8, new §8c, new §11, and the open decisions at the end).

Genuinely-undecided items are collected once at the end under
[Cross-cutting open decisions](#cross-cutting-open-decisions).

---

## 1. Custom resource (GVR / kind)

| Field | Value |
|---|---|
| **Group** | `anki.ankimcp.ai` |
| **Version** | `v1alpha1` |
| **Kind** | `AnkiInstance` |
| **Singular / plural / shortName** | `ankiinstance` / `ankiinstances` / `anki` |
| **Scope** | Namespaced (all instances in the [instances namespace](#4-namespace)) |
| **Full GVR string** | `anki.ankimcp.ai/v1alpha1, Resource=ankiinstances` |

There is **exactly one `AnkiInstance` per user.** (The earlier `ankimcp.io` group used in an
early activator draft is **wrong** — the operator's `anki.ankimcp.ai` wins.)

## 2. CR field ownership

**DECIDED 2026-07-11 (user): single-writer v1.** The activator is shelved (v1 does not deploy
it), and with it the two-writer contract below. **The lifecycle service is the ONLY writer of the
CR `spec`**; the operator still writes `status.*` only (status subresource, field owner
`anki-operator`) and reconciles the children. The v1 ownership table:

| Field | Owner (field manager) | Meaning |
|---|---|---|
| `spec.user` | **lifecycle** (`anki-lifecycle`) | tenant identity = keycloakId; **immutable**, `== metadata.name` |
| `spec.replicas` (0↔1) | **lifecycle** (`anki-lifecycle`) | wake (dashboard Start / tunnel-triggered ensure-connected) and idle-TTL sleep — the lifecycle service owns the idle TTL in v1 |
| `spec.suspended` (bool) | **lifecycle** (`anki-lifecycle`) | administrative power gate (dashboard on/off, tier lapse) |
| `spec.restartedAt` (RFC3339 timestamp) | **lifecycle** (`anki-lifecycle`) | **NEW 2026-07-11** — restart nonce: the operator copies the value into the StatefulSet pod template as an annotation (the `kubectl rollout restart` pattern) → pod recreated, PVC untouched. Used for tunnel reconnect/takeover (formerly also hkey pickup — credential channel removed in v1, §8) |
| data-fate field (e.g. `spec.dataRetention: Delete\|Retain`) | **lifecycle** (`anki-lifecycle`) | **NEW 2026-07-11** — PVC fate on CR delete; the **operator** deletes the PVC (finalizer flow) when the CR is deleted with fate=Delete. Exact field name is owned by the operator spec. The lifecycle service keeps **zero workload-object RBAC verbs** (its Role stays CRs + Secrets only) |
| `spec.image`, other future toggles | **lifecycle** (`anki-lifecycle`) | image pin, per-addon config |
| `status.*` | **operator** (`anki-operator`) | via the status subresource only (never bumps generation) |
| children (StatefulSet, Service, Secret *mount*) | **operator** | reconciled from spec; nobody else writes them |

`suspended` and `replicas` remain **two fields on purpose** even under a single writer: they carry
different intents the dashboard shows as distinct user-facing states — **off** (power gate) vs
**sleep** (replicas 0) vs **on** ([ARCHITECTURE §16](./ARCHITECTURE.md)).

> **SUPERSEDED (2026-07-11) — historical two-writer contract, revived if the activator returns:**
> two writers touched the CR spec and were forbidden from sharing a field (single-writer-per-field
> as the control-plane invariant). The operator and lifecycle used SSA Apply; the **activator used
> a minimal merge-patch carrying only `spec.replicas`** (blessed by requirements-activator §6.2),
> field manager `anki-activator` (verified 2026-07-10: patch body `{"spec":{"replicas":N}}`,
> test-asserted). In that table `spec.replicas` was **activator-owned** (wake / idle-suspend
> within the power gate); everything else was as above. The activator's `spec.replicas` ownership
> is shelved with the component.

**Effective replicas (operator computes the StatefulSet's replicas):**

```
effectiveReplicas = (spec.suspended == true) ? 0 : spec.replicas
```

- The operator never writes back to any `spec` field. Setting the *StatefulSet's* replicas is not
  "flipping `spec.replicas`".
- `suspended=true` forces the pod to 0 regardless of `replicas`; clearing it returns the pod to
  whatever `replicas` currently is (an admin re-enable does not force a wake).
- The power gate is **`spec.suspended` (bool)** — the earlier lifecycle-doc proposal
  `spec.desiredState: Running|Stopped` is **replaced** by this.

## 3. Naming scheme

All names derive from the **keycloakId** (Keycloak `sub` / `users.keycloak_id`), a UUID that is
already DNS-1123-safe. keycloakId is the routing/identity key everywhere — **not** the AnkiWeb
username.

| Object | Name |
|---|---|
| `AnkiInstance` CR (`metadata.name`) | `<keycloakId>` (the raw UUID) |
| `spec.user` | `<keycloakId>` (same value) |
| StatefulSet | `anki-<keycloakId>` |
| headless Service | `anki-<keycloakId>` |
| credentials Secret | `anki-<keycloakId>` |
| B2/rclone env Secret | `anki-<keycloakId>-b2` |
| Pod (STS ordinal) | `anki-<keycloakId>-0` |
| Stable pod DNS | `anki-<keycloakId>-0.anki-<keycloakId>.anki-instances.svc.cluster.local` |
| Per-user B2 prefix | `anki/<keycloakId>/` |

The lifecycle service maps the (stable) keycloakId to these names deterministically; the
~~activator~~ **VNC gateway** (2026-07-11 — the activator is shelved) resolves
`identity → pod DNS` by the **same** convention (routing is a naming convention, not stored data
— the gateway has no k8s API access at all: it dials
`anki-<keycloakId>-0.anki-<keycloakId>.anki-instances.svc.cluster.local:6080` and fails closed on
a dead target). **No instance for an identity ⇒ fail closed** (404 / close WS, never create or
wake — waking is manual from the dashboard, or tunnel→lifecycle for MCP; see §7 and
[ARCHITECTURE §16](./ARCHITECTURE.md)).

## 4. Namespace

**`anki-instances`** — holds every `AnkiInstance` CR, per-user StatefulSet/Pod/Service, and per-user
credentials Secret. **PSA label: `privileged`** ([ARCHITECTURE §10 layer 3](./ARCHITECTURE.md),
DECIDED 2026-07-10) — the default per-user rclone sidecar is a `privileged` container (Bidirectional
mount propagation requires it; see [§10 pod shape](#10-per-user-pod-shape-media-mount)). The
namespace holds ONLY Anki pods (relaxed admission and hostile-by-assumption pods are the same set)
and is walled in by the default-deny `CiliumNetworkPolicy`. The namespace, its PSA label, and the
network policy are **infra-owned** (created by the infrastructure/argocd app, not the operator).

**NetworkPolicy deltas (DECIDED 2026-07-11 — tunnel pivot, [ARCHITECTURE §16](./ARCHITECTURE.md)):**

- **Ingress:** only from the **VNC gateway** (to `vnc-ws` 6080). The former activator ingress rule
  is shelved with the activator; nothing dials the pod's MCP port 3141 (§7).
- **Egress:** DNS + AnkiWeb + the B2 endpoint stay, **plus the in-cluster tunnel service** — the
  pod's AnkiMCP add-on opens an **outbound** WebSocket to the tunnel's internal `/connect` URL
  (§8c). Everything else stays denied by omission.

## 5. Labels

Applied by the operator to every child and used as the pod selector and the network-policy
`endpointSelector`:

| Label | Value |
|---|---|
| `app.kubernetes.io/name` | `anki-instance` |
| `app.kubernetes.io/managed-by` | `anki-operator` |
| `app.kubernetes.io/instance` | `anki-<keycloakId>` |
| `anki.ankimcp.ai/user` | `<keycloakId>` |

## 6. Node pinning & storage

- **Node-pin label (Cloud-Volume-capable nodes):** `ankimcp.ai/storage: hcloud-volumes`. Stateful
  Anki pods `requiredDuringScheduling` node-affinity onto nodes carrying it. **This label must be
  created by infra** on the Hetzner Cloud nodes (Cloud Volumes cannot attach to dedicated servers);
  it does not exist today.
- **StorageClass:** `hcloud-volumes` (Hetzner Cloud Volume via `hcloud-csi`).
- **PVC:** `10Gi` (Hetzner's minimum volume size), access mode **`ReadWriteOncePod`**,
  `persistentVolumeClaimRetentionPolicy: { whenDeleted: Retain, whenScaled: Retain }`.
- **rclone VFS cache:** on the PVC, LRU cap **default 6GB** (leaves headroom for DBs + WAL + addons
  on the 10GB PVC).

## 7. Ports & Service

The per-user Service is **headless** (`clusterIP: None`), selects the [labels](#5-labels), and
exposes — **in v1, only `vnc-ws`** (DECIDED 2026-07-11):

| Port name | Container port | Transport | Notes |
|---|---|---|---|
| `mcp` | **3141** | MCP-over-HTTP (AnkiMCP add-on) | ~~activator forwards `/mcp` here; **forward-readiness gate**~~ **NOT a Service port in v1 (2026-07-11):** nothing dials inbound MCP — MCP rides the add-on's **outbound** tunnel connection (§8c). The "forward-readiness gate" concept is **moot**; the MCP readiness signal = "add-on connected to the tunnel". (3141 may remain an in-pod listener/probe target — image spec's call.) |
| `vnc-ws` | **6080** | WebSocket (websockify/noVNC) | the **VNC gateway** (2026-07-11 — replaces the activator's VNC door) forwards `/vnc` here |

Not Service ports (localhost-only inside the pod): x11vnc RFB **5900**, rclone rc **5572**.
AnkiConnect (8765) is **not** exposed in v1.

## 8. Credentials Secret

> **Sync-credentials channel REMOVED in v1 (2026-07, platform decision).** The former
> `sync-credentials.json` data key (the ci-buddy hkey contract, old REQUIREMENTS §B.2) is
> **gone**: the lifecycle service performs no password→hkey exchange and writes no sync
> credentials, and ci-buddy never touches them. AnkiWeb login is **user-managed over VNC** and
> persists in `prefs21.db` on the PVC (wiped only with the instance + PVC —
> [../REQUIREMENTS.md](../REQUIREMENTS.md) Part B). The Secret object remains, carrying only the
> tunnel token (§8c).

| Aspect | Value |
|---|---|
| Secret name | `anki-<keycloakId>` (namespace `anki-instances`) |
| Written by | **lifecycle service** (the Secret *object*) |
| Mounted by | **operator** (whole-volume mount, **never `subPath`**, `optional: true`) |
| Read by | **AnkiMCP add-on** in the pod (tunnel token, §8c) |
| Mount dir | `/run/ankimcp` |
| Files | `/run/ankimcp/tunnel-credentials.json` (data key = filename; §8c) |
| Mode / owner | `defaultMode: 0400` + `fsGroup` = **`10001`** (the image's pinned Anki uid/gid, Dockerfile `ANKI_UID`/`ANKI_GID`), so the unprivileged app can read it |

- **Mounted into the anki container ONLY**, as a file. This Secret holds a bearer token and is
  **never `envFrom`'d into any container** — envFrom'ing it would inject the token JSON into the
  privileged rclone sidecar's environment (the data-key filename **is** a valid env-var name; the
  kubelet does **not** skip it). The B2/rclone env creds ride a **separate `-b2` Secret** (§9).

### 8b. B2/rclone env Secret (`anki-<keycloakId>-b2`)

| Aspect | Value |
|---|---|
| Secret name | `anki-<keycloakId>-b2` (namespace `anki-instances`) |
| Written by | **lifecycle service** (the Secret *object*) |
| Consumed by | **operator** as `envFrom` into the **rclone sidecar ONLY** (`optional: true`) |
| Read by | **rclone** in the sidecar (`RCLONE_CONFIG_B2_*` are rclone's own env interface) |
| Required keys | `RCLONE_CONFIG_B2_TYPE`, `RCLONE_CONFIG_B2_PROVIDER`, `RCLONE_CONFIG_B2_ACCESS_KEY_ID`, `RCLONE_CONFIG_B2_SECRET_ACCESS_KEY`, `RCLONE_CONFIG_B2_ENDPOINT`, `B2_BUCKET` |
| Optional keys | `B2_PREFIX` (per-user prefix, e.g. `anki/<keycloakId>`) |

- The `RCLONE_CONFIG_B2_*` prefix matches the rclone remote name `RCLONE_REMOTE=b2` (image default);
  `rclone-mount.sh` builds `b2:${B2_BUCKET}/${B2_PREFIX}` and **dies on an empty `B2_BUCKET`**, so
  `B2_BUCKET` is mandatory. The key names above are the **`s3`-at-B2** backend set; if the still-open
  backend choice (decision #5) lands on native `b2`, the `RCLONE_CONFIG_B2_*` value set changes
  (e.g. `_ACCOUNT`/`_KEY` instead of `_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY`) — `envFrom` is
  backend-agnostic and picks up whatever the lifecycle service writes.
- **Why a second Secret:** keeping the B2 env creds out of the credentials Secret is what lets the
  operator `envFrom` them into the privileged sidecar **without** dragging the tunnel token into
  that sidecar's environment (§8).

### 8c. Tunnel credentials — second data key in `anki-<keycloakId>` (PINNED 2026-07-11)

**DECIDED 2026-07-11 (user).** The credentials Secret `anki-<keycloakId>` (§8) carries the
data key `tunnel-credentials.json` — whole-volume mount at `/run/ankimcp`, **anki container
only, never `envFrom`'d** into any container (§8/§8b). (Originally a *second* key beside
`sync-credentials.json`; since the sync-credentials removal, §8, it is the only key.) It
authenticates the pod's baked AnkiMCP add-on to the SaaS **tunnel** service — the MCP door for
hosted instances ([ARCHITECTURE §16](./ARCHITECTURE.md)).

```json
{ "v": 1, "token": "<opaque internal bearer>", "tunnelUrl": "wss://<internal-tunnel-host>/connect", "user": { "id": "<keycloakId>" } }
```

| Rule | Value |
|---|---|
| `v` | strict `1`; shape changes bump it; the add-on ignores unknown keys |
| `token` | minted by the **lifecycle service**; presented **verbatim** as `Authorization: Bearer` on the add-on's WS upgrade to the tunnel; the tunnel validates it against the lifecycle signing key — which also marks the connection as **"hosted"** |
| `tunnelUrl` | lifecycle **ALWAYS** writes it (env-specific, in-cluster internal host; any add-on fallback is unused) |
| `user.id` | display-only |
| tier / entitlement data | **NEVER in the file** — the pod is untrusted; all checks are server-side |
| rotation | replace the file + restart the pod (`spec.restartedAt`, §2) |

## 9. B2 key / prefix scheme

- **Per-user application key**, `namePrefix`-restricted to `anki/<keycloakId>/` and
  `bucketId`-restricted to the shared anki bucket ([ARCHITECTURE §10 layer 2](./ARCHITECTURE.md)):
  full pod compromise yields only that user's own media.
- Reuse the **`media-library-b2-provisioning.md`** bucket-scoped-key + per-user-prefix model
  (infra repo) — extended from per-user *paths* to per-user *keys*; **do not invent a second one**.
- Minted / rotated / revoked by the **lifecycle service** (holds a `writeKeys`+`deleteKeys`
  key-manager credential). The `applicationKey` secret is shown once by B2, written straight into
  the [B2 env Secret](#8b-b2rclone-env-secret-anki-keycloakid-b2), and never stored
  — only the `applicationKeyId` handle is persisted.
- The B2 endpoint (`s3.eu-central-003.backblazeb2.com` for the S3 backend) must be allowed by the
  namespace egress `CiliumNetworkPolicy` (`toFQDNs`).
- **Delivery to the pod (DECIDED 2026-07-10, downstream of the sidecar decision):** the per-user
  rclone config (`RCLONE_CONFIG_B2_*` keys + `B2_BUCKET`/`B2_PREFIX`) is delivered as **env `envFrom`
  a SEPARATE per-user Secret** `anki-<keycloakId>-b2` ([§8b](#8b-b2rclone-env-secret-anki-keycloakid-b2))
  into the **rclone sidecar** container ([§10](#10-per-user-pod-shape-media-mount)). It is
  backend-agnostic (the lifecycle service chooses the native-`b2` vs `s3`-at-B2 key set — that choice
  is still open, decision #5 below); `envFrom` picks up whatever keys were written.
- **Secret separation is load-bearing (not cosmetic):** the B2 env creds live in `anki-<keycloakId>-b2`,
  NOT in the credentials Secret `anki-<keycloakId>` (§8). The credentials Secret's
  `tunnel-credentials.json` data key **is** a valid env-var name and the kubelet does **not** skip it —
  so if the sidecar `envFrom`'d the credentials Secret, the token JSON would land in the privileged
  sidecar's environment. The credentials Secret is therefore file-mounted into the anki container only,
  and the sidecar `envFrom`s the `-b2` Secret only.

## 10. Per-user pod shape (media mount)

**DECIDED 2026-07-10 ([ARCHITECTURE §10 layer 3](./ARCHITECTURE.md)): per-user rclone SIDECAR.** The
operator renders this shape by default (`--media-mount-mode=sidecar`); `external` keeps a
single-container CSI-provided path possible. The pod (StatefulSet template) is:

| Container | Kind | Privilege | Media mount | Role env |
|---|---|---|---|---|
| **rclone** | **native sidecar** (init container, `restartPolicy: Always`) | **`privileged: true`** | mounts the shared `media` emptyDir **`Bidirectional`**; FUSE-mounts B2 there | `CONTAINER_ROLE=rclone-sidecar`, `MEDIA_MOUNT_MODE=internal` |
| **anki** | app container | unprivileged (drop `ALL`, no privesc, **no added caps**) | mounts the same `media` emptyDir **`HostToContainer`** | `MEDIA_MOUNT_MODE=external` |

- **Why the sidecar is `privileged` (honest):** its rclone FUSE mount must propagate OUT to the anki
  container, which requires `mountPropagation: Bidirectional`, and **Kubernetes requires a container
  using Bidirectional propagation to be `privileged: true`** — `SYS_ADMIN` + `/dev/fuse` alone are
  **not** accepted (kubernetes/kubernetes PR #117812 to relax this was closed unmerged). This is the
  accepted cost that makes the namespace PSA-`privileged` (§4). Blast radius is bounded: the sidecar
  runs as the non-root anki uid, carries no k8s creds, drops into the same default-deny network
  policy, and the anki container stays fully unprivileged.
- **Shared media volume:** an `emptyDir` named `media` at `/media/b2`, mounted in both containers with
  the propagation modes above. The rclone FUSE mount uses `--allow-other` (with `user_allow_other` in
  the image's `/etc/fuse.conf`) so the anki container can read/write across the propagation boundary.
- **rclone VFS cache on the PVC** ([§4-durability](./ARCHITECTURE.md)): the sidecar also mounts the
  profile PVC (`subPath: .rclone-cache`) at `/data/.rclone-cache`, so un-flushed write-back survives a
  crash and resumes on next start.
- **Native-sidecar ordering** (GA k8s 1.33, on-by-default ≥1.29; cluster targets 1.36): the sidecar
  starts — and mounts — before the anki container and is terminated **after** it. So on downscale the
  anki container's `preStop` quits Anki (close-sync) and drains the write-back queue via the sidecar's
  rc over pod-shared localhost **while rclone is still alive**; the sidecar's own `preStop` then does a
  final idempotent drain + unmount. This makes the drain ordering **deterministic, not racy**.
- **`terminationGracePeriodSeconds` invariant (default `180`):** the preStop budget is **sequential**
  across the two containers, so grace **MUST** satisfy
  `grace >= ANKI_QUIT_TIMEOUT + 2*RCLONE_DRAIN_TIMEOUT + 30`. With the image defaults
  (`ANKI_QUIT_TIMEOUT=60`, `RCLONE_DRAIN_TIMEOUT=45` — anki-side quit + anki-side drain + sidecar-side
  drain) the worst case is ~150s; 180 leaves margin. Drift risk: the timeouts live in the image's
  `scripts/lib.sh`, the grace lives in the operator's `internal/config` — raise both together or the
  kubelet SIGKILLs mid-drain when Anki stalls on a sync dialog.
- **Pod `securityContext` uid/gid = `10001`:** `runAsUser`/`fsGroup` MUST equal the image's pinned
  Anki uid/gid (Dockerfile `ANKI_UID`/`ANKI_GID` = `10001`). The pod securityContext overrides the
  image, so a mismatch runs everything as the wrong uid → the `0400` credentials Secret (chowned to
  `fsGroup`) is unreadable and FUSE `--allow-other` ownership breaks (§8).

## 11. Instance status enum (added 2026-07-11)

**The canonical dashboard-facing instance status enum** — pinned here 2026-07-11 to end the
divergence between the lifecycle projection (requirements-lifecycle-service §10) and the
dashboard's `InstanceStatus` type (requirements-dashboard-gui §4) (`Waking`≠`Starting`,
`Failed`≠`Error`, `Resetting`/`Deleting` unmapped). Both docs MUST use exactly this set:

`None | Provisioning | Stopped | Suspended | Starting | Running | Stopping | Deleting | Error`

| Value | Meaning |
|---|---|
| `None` | no instance (no CR; dashboard shows the create CTA) |
| `Provisioning` | create saga / first-time setup in progress |
| `Stopped` | user/admin power gate "off" (`spec.suspended: true`, §2) |
| `Suspended` | idle sleep — replicas 0, gate off, PVC retained |
| `Starting` | 0→1 in progress — covers wake AND restart-in-progress (a `spec.restartedAt` bump, §2) |
| `Running` | pod Ready |
| `Stopping` | graceful downscale in flight |
| `Deleting` | teardown saga running (lifecycle §5.6) |
| `Error` | terminal failure needing user action |

**Mapping to the user-facing three-state power model ([ARCHITECTURE §16.4](./ARCHITECTURE.md)):**
**off** = `Stopped`; **sleep** = `Suspended`; **on** = `Running`; everything else is transitional
(rendered as a spinner toward the target state) or `None` (no instance).

---

## Cross-cutting open decisions

Genuinely-open items that touch more than one component (or await a human decision). Component
docs point here rather than each carrying a divergent guess.

1. ~~**`sid` → identity resolution for the VNC WebSocket.**~~ **RESOLVED (2026-07-11, user):
   option (b) — a short-lived, single-use, SIGNED VNC ticket**, minted by the SaaS **`apps/api`**
   (which owns browser sessions); the **VNC gateway** (the activator's v1 replacement for the VNC
   door) verifies it **locally with a public key** — no per-connect introspection call, no DB, no
   k8s access. Routing is **same-origin `/vnc` path** via the existing Cloudflare Tunnel ingress
   (no `vnc.` subdomain), so the cross-origin `SameSite`/cookie question **dissolves**. See
   dashboard §6.2; the gateway lives in the `anki-mcp-saas` stack (own app vs endpoint on an
   existing service = implementation detail).

2. ~~**MCP auth: terminate vs passthrough.**~~ **MOOT (2026-07-11):** the activator is shelved and
   MCP no longer traverses any in-cluster proxy — it rides the add-on's **outbound** tunnel
   connection (§8c). LLM-side auth stays the tunnel's existing Bearer validation at `/mcp`;
   pod-side auth is the lifecycle-minted tunnel token (§8c). Port 3141 is not Service-exposed in
   v1 (§7).

3. **Clean-quit trigger (graceful downscale).** It is unverified whether headless `aqt` runs its
   close-time auto-sync on a bare `SIGTERM`. Preferred robust mechanism: an **AnkiMCP add-on
   shutdown action** that calls `mw.close()` on the Qt main thread, invoked by `preStop`. Do not
   ship a `SIGKILL`-equivalent that skips close-sync. **headless-anki-image §7.2/§14.1** (may need a
   small `shutdown` action in `anki-mcp-server-addon`).

4. ~~**FUSE privilege model — sidecar vs DaemonSet.**~~ **DECIDED 2026-07-10 (user):
   per-user rclone SIDECAR** ([ARCHITECTURE §10 layer 3](./ARCHITECTURE.md), pod shape in
   [§10](#10-per-user-pod-shape-media-mount)). Rationale: it natively satisfies §4 (VFS cache on the
   pod's PVC) with mount-lifecycle == pod-lifecycle and per-user blast radius; stock CSI-rclone was
   ruled out (node-local cache breaks write-back durability; shared-plugin blast radius) and a custom
   DaemonSet owned CSI-grade remount complexity forever. **Accepted cost:** the sidecar is
   `privileged` (Bidirectional propagation requires it — verified, PR #117812 closed unmerged), so the
   namespace is PSA-`privileged` (§4). The image is agnostic via `MEDIA_MOUNT_MODE` + `CONTAINER_ROLE`;
   the operator renders it via `--media-mount-mode=sidecar` (default), keeping `external`/CSI possible.

5. **B2 backend shape (delivery DECIDED; backend still open).** Delivery is **settled**: the per-user
   rclone config is env `envFrom` the separate `-b2` Secret into the sidecar (§8b, §9, §10). Still
   open: native `b2` backend vs `s3`-at-B2-endpoint (which `RCLONE_CONFIG_B2_*` key set the lifecycle
   service
   writes) — align with whatever `media-library-b2-provisioning.md` provisions. The operator's
   `envFrom` is backend-agnostic, so this does not block it. **lifecycle §6.2/§15**,
   **headless-anki-image §5.2/§14.2**.

6. ~~**Lifecycle service home.**~~ **RESOLVED (2026-07-11, user): atomic service inside
   `anki-mcp-saas`** (like tunnel / studio-*) → the transport binding is **NATS behind the BFF**
   (lifecycle §5.1 primary). **lifecycle §15.1.**

7. ~~**AnkiWeb `sync_login` wire mechanism.**~~ **RESOLVED (2026-07-11, verified against Anki
   source at tag 25.09.2): implement DIRECTLY in Node** (no shelling to Anki).
   `POST {endpoint}sync/hostKey` (default `https://sync.ankiweb.net/`), headers
   `anki-sync: {"v":11,"k":"","c":"<client-ver>","s":"<random>"}` +
   `content-type: application/octet-stream`, body = zstd(JSON `{"u":username,"p":password}`),
   response = zstd(JSON `{"key":"<hkey>"}`) with an `anki-original-size` header; **403 = bad
   credentials**. Protocol **v11**, stable since 2023 (server accepts the range [8,11] —
   hardcode 11); zstd via `node:zlib` (Node ≥ 22.15). The 308 sharding redirects apply to *later
   sync calls only*, not login. Full shape in **lifecycle §6.1** (its
   `ANKIWEB_SYNC_LOGIN_MODE` config option is dropped — only one mode).
