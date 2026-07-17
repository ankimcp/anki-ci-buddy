# requirements — lifecycle service (control-plane management API)

> **HANDOFF (2026-07-10):** this component is implemented by the **`anki-mcp-saas` agent**, not
> in this (private, internal) repo. This doc is the self-contained spec. Before implementing,
> read **`contracts.md`** (shared GVR `anki.ankimcp.ai/v1alpha1`, CR field ownership — this
> service owns `spec.user`/`spec.suspended`/`spec.image` + writes the credentials Secret, ~~never
> `spec.replicas`~~) and **`ARCHITECTURE.md`** §2/§6/§8. The service is **closed-source** (folds
> into the SaaS). The operator ~~+ activator~~ it talks to already exist in this repo's
> `operator/` ~~and `activator/`~~ (built + reviewed) — treat their contracts as fixed.

> **REVISED 2026-07-11 (user — [ARCHITECTURE §16](./ARCHITECTURE.md), [contracts.md](./contracts.md)):**
> (a) **Home RESOLVED:** atomic service inside `anki-mcp-saas`; transport = **NATS behind the
> BFF** (§5.1 primary binding). (b) **The activator is SHELVED for v1** — this service is now the
> **single writer of the entire CR spec**, *including* `spec.replicas` (wake §5.7 + idle-TTL sleep
> §5.8) and the new `spec.restartedAt` restart nonce (§5.5); the "never `spec.replicas`" lines in
> this doc are superseded where marked. (c) **New responsibilities:** mint the pod→tunnel bearer
> token (second Secret data key, §6.4 / contracts.md §8c); serve the tunnel's **ensure-connected**
> wake call (§5.7); own the **idle TTL** (§5.8); the **`pro` tier gate + lapse** flow (§5.9).
> (d) §6.1 (AnkiWeb hkey wire shape) and §6.2 (per-user B2 keys) are now **verified** — see the
> rewritten sections; `ANKIWEB_SYNC_LOGIN_MODE` is dropped from §11.

> **REVISED 2026-07 (platform decision): the AnkiWeb GUI credential channel is REMOVED from
> v1.** This service **no longer** performs the password→hkey exchange (§6.1), no longer writes
> the `sync-credentials.json` Secret data key or bumps a credential `serial` (§6.3), and never
> restarts a pod for hkey pickup. The user logs into AnkiWeb **manually inside Anki over VNC**;
> the login persists in `prefs21.db` on the PVC and is wiped only when the instance + PVC are
> deleted ([../REQUIREMENTS.md](../REQUIREMENTS.md) Part B; ARCHITECTURE §6). The
> `ankiwebLogin` API, the `credential` DB table, and the "credentials active within a minute"
> UX are dropped with it. The credentials Secret `anki-<keycloakId>` remains, carrying **only**
> the tunnel token (§6.4, contracts.md §8/§8c). Sections below describing the hkey flow are
> retained as history and marked where they occur; the §6.1 AnkiWeb wire-shape research stays
> valid as protocol knowledge.

Implementation spec for the **lifecycle service**: the control-plane front door of the
Anki-as-a-Service platform. It translates authenticated user (and admin) intents into
**`AnkiInstance` CR writes** and **k8s Secret writes**, and it owns the credential
exchange no other component may hold (the per-user
B2 application-key provisioning; formerly also the AnkiWeb password → hkey mint — removed in
v1, see the 2026-07 banner). Written for an implementing agent.

Authoritative inputs (do **not** contradict): platform
[ARCHITECTURE.md](./ARCHITECTURE.md) (esp. §2, §6, §8, §9, §10 layer 2, §13, §14), the shared
[contracts.md](./contracts.md) registry (GVR, CR field ownership, naming, namespace, labels,
Secret, B2 key/prefix). Anything the authoritative docs leave unresolved is
captured in **§15 Open questions**, not decided here.

**Power gate (decided — [contracts.md §2](./contracts.md)):** the lifecycle-owned administrative
on/off is the CR field **`spec.suspended` (bool)**. This replaces the `spec.desiredState:
Running|Stopped` proposal earlier in this doc; semantics are unchanged (lifecycle owns the gate,
~~activator owns `spec.replicas`~~ — 2026-07-11: lifecycle owns `spec.replicas` too, single-writer
v1 — operator computes `effectiveReplicas = suspended ? 0 : replicas`).

> ~~**Home is undecided (ARCHITECTURE §14, §2, §13).**~~ **RESOLVED (2026-07-11, user):** the
> service lands as an **atomic service inside `anki-mcp-saas`** (like tunnel / studio-*), so the
> transport binding is the **primary** one in §5.1 — NATS RPC behind the `apps/api` BFF. The
> standalone-HTTP alternative in §5.1 is dead for v1 (retained for context only). The spec's
> API-first framing stands.

---

## 1. Goal & scope

**Goal:** a small stateful control-plane service that is the *only* writer of a user's
`AnkiInstance` CR ~~(except `spec.replicas`, owned by the activator)~~ **(2026-07-11: including
`spec.replicas` — single-writer v1, contracts.md §2)**. ~~And the *only* holder of that user's
AnkiWeb password during the hkey exchange~~ (2026-07: no component holds the AnkiWeb password —
the credential channel is removed; login is manual over VNC).

**In scope**
- REST/RPC management API: **create / delete / stop / start / reset / get-status** an
  instance. ~~And **AnkiWeb re-login**~~ (2026-07: removed with the credential channel).
- **(added 2026-07-11)** The **pod→tunnel credential**: mint the per-instance opaque bearer token
  and write it as the second Secret data key `tunnel-credentials.json` (§6.4, contracts.md §8c).
- **(added 2026-07-11)** **Wake / ensure-connected** (§5.7): serve the tunnel's NATS call; run the
  instance/tier/power-gate checks and pick the lever (`spec.replicas: 1` or a `spec.restartedAt`
  bump).
- **(added 2026-07-11)** **Idle TTL + downscale trigger** (§5.8): track activity (tunnel
  per-request quota events + VNC-gateway session signals) and set `spec.replicas: 0` on expiry.
- **(added 2026-07-11)** **Tier gate & lapse** (§5.9): `pro`-gated create; consume
  `subscription.tier_changed`; immediate power gate + 1-month grace + delete saga on lapse.
- ~~The **AnkiWeb credential flow**~~ — **REMOVED in v1** (2026-07 banner): no
  password→hkey mint, no sync-credentials Secret key, no serial.
- The **B2 credential flow**: provision a **per-user application key restricted to the
  user's prefix**, deliver it where the pod/CSI expects it, rotate + revoke it on delete.
- **Provisioning orchestration** (signup → B2 key → Secret → CR), idempotency,
  partial-failure recovery, rate limiting.
- **Status/events** projection for the dashboard (§9 of ARCHITECTURE) and the AGPL
  source-link data.
- The **admin surface** (list all, force-stop), separated from the user surface.

**Out of scope (other components — do NOT build here)**
- ~~**Anything on `spec.replicas`.** Wake, hold, forward, idle-TTL, graceful downscale, and
  scale-to-zero are the **activator's** exclusive job (ARCHITECTURE §2, §7, decision 7).~~
  **SUPERSEDED (2026-07-11):** wake and idle-TTL scale-to-zero are now THIS service's job
  (§5.7/§5.8). What stays out of scope: **request-hold and forwarding** (the tunnel holds and
  forwards MCP), and the **graceful part of downscale** (setting `replicas: 0` is sufficient —
  the pod's `preStop` hooks own quit/drain, contracts.md §10).
- **Creating StatefulSets / PVCs / Services / Secret mounts.** That is the **operator**
  reconciling the CR (ARCHITECTURE §8). The lifecycle service writes intent on the CR and
  writes the Secret *object*; it never renders workload manifests. **(2026-07-11: PVC deletion is
  likewise operator-owned, via the CR data-fate field — §5.6.)**
- **Proxying any user traffic** (MCP / HTTP / VNC-WS). Data plane only (~~activator~~
  2026-07-11: the **tunnel** for MCP, the **VNC gateway** for VNC).
- Delivering the credentials file *into the pod filesystem* — the operator/headless-anki
  side owns the Secret→file mechanics; the lifecycle service only writes the **Secret
  object** (tunnel token, §6.4).
- Sync execution — the AnkiMCP add-on owns it. Sync *credentials* are user-managed
  (manual VNC login, ci-buddy REQUIREMENTS Part B) — no component reads or applies them.

---

## 2. Responsibility boundary (hard rules)

These are invariants; a change that violates one is a bug, not a feature.

1. **Writes `AnkiInstance` CRs and Secrets only.** No StatefulSet/PVC/Service/Deployment
   writes — ever. (ARCHITECTURE §2, §8.) **(2026-07-11: PVC deletion resolved as a CR data-fate
   field the operator honors — §5.6 — so this Role keeps zero workload-object verbs, §9.)**
2. ~~**Never writes `spec.replicas`.** That field has exactly one writer: the activator.~~
   **SUPERSEDED (2026-07-11, single-writer v1 — contracts.md §2):** this service IS the only
   `spec.replicas` writer (wake §5.7, idle sleep §5.8). The surviving invariant: explicit user
   **stop** is still expressed on the **separate `spec.suspended` field** (§5.4) — `replicas`
   carries only wake/sleep, never "off". The two fields stay separate because **off** and
   **sleep** are distinct user-facing states (dashboard model, ARCHITECTURE §16.4).
3. **Never touches a running pod.** All controls follow the platform's single pattern:
   **patch desired state → let the operator reconcile** (ARCHITECTURE §9). No
   `kubectl exec`, no live surgery, no in-pod filesystem writes.
4. **This service never sees the AnkiWeb password** (2026-07: the credential channel is
   removed — login happens inside the pod over VNC; formerly the password was
   request-scoped in-memory for the `sync_login` call only, ARCHITECTURE §6).
5. **The pod carries zero k8s credentials.** This service holds the k8s API access, scoped
   to `AnkiInstance` CRs + Secrets in the anki namespace (§9). The pod gets a mounted
   Secret and nothing else (ARCHITECTURE §10 layer 2, decision 9).

---

## 3. Conventions this service reuses (anki-mcp-saas)

Studied from the atomic services `studio-media-library`, `studio-knowledge-map`, and
`tunnel`, plus the `apps/api` BFF. The lifecycle service **must** follow these rather than
invent new patterns.

| Concern | Convention (cited) | Applies here |
|---|---|---|
| **Framework** | NestJS + Pino logger (`@repo/logger`); `main.ts` bootstraps, `bufferLogs`+`flushLogs`, `enableShutdownHooks()` (`apps/tunnel/src/main.ts`, `apps/*/src/main.ts`) | same |
| **Transport** | Backend atomic services are **NATS-only** (health HTTP aside); the **`apps/api` BFF** is the browser front door (oRPC over WebSocket, Keycloak PKCE session) and forwards to services on **per-user NATS subjects** (`apps/api/src/api-gateway/websocket/router.ts`) | §5 |
| **Subject namespace** | `<svc>.rpc.user.*` = user-facing, identity is `keycloakId` **stamped by the edge, never caller-supplied**; `<svc>.rpc.internal.*` = **reserved for admin/internal**, gateway never forwards user traffic there (`apps/studio-media-library/src/nats/subjects.ts`, `packages/media-library-contract/src/subjects.ts`) | §5, §8 |
| **Request envelope** | `ServiceRequest` = method + input + `keycloakId: z.uuid()`; reply is a discriminated `ok` union carrying a **stable domain error code**, never internals (`packages/media-library-contract/src/transport.ts`) | §5 |
| **Contract package** | Wire types (methods manifest, subjects, envelopes) live in a shared `@repo/<svc>-contract` package imported by both edges + backend — no string copies (`packages/media-library-contract`) | new `@repo/anki-lifecycle-contract` |
| **Postgres** | Own dedicated DB on `pg-shared`, discrete `DATABASE_*` env pieces assembled by `@repo/config` `getDatabaseConfig()`; **Kysely** client (`@repo/database` `createKyselyClient`); migrations via `kysely/migration` `Migrator` run by `src/database/migrate.ts` in a **migrate init-container** (`apps/studio-media-library/src/database/migrate.ts`) | §4 |
| **Config** | `src/config/configuration.ts` with a `validate*Env()` that **fail-fasts at boot listing every missing var**; positive-int knobs required, no silent defaults (`apps/studio-media-library/src/config/configuration.ts`) | §11 |
| **Object storage** | Single S3 seam over `@aws-sdk/client-s3` + `getObjectStorageConfig()`, B2 needs `forcePathStyle`, per-user path prefix `media/<ownerId>/…` (`apps/studio-media-library/src/storage/object-storage.service.ts`, `.../media-library/path.util.ts`) | §6.2 |
| **Users lookup** | `@repo/users-client` (`getUserWithTier`, roles) over `USERS_SERVICE_URL`+`USERS_SERVICE_SECRET` (`packages/users-client`) | §8, §7 |
| **Observability** | `instrumentation.ts` (OTLP traces) imported **first** in `main.ts`; `OTEL_*` env required unless `OTEL_ENABLED=false`; health checks module (`apps/tunnel/src/health`) | §12 |
| **Shutdown** | `OnApplicationShutdown` service flushing telemetry last; NATS loop + DB pool close in their own `OnModuleDestroy` (`apps/*/src/shutdown/*-shutdown.service.ts`) | §12 |
| **Deploy** | Per-service `Dockerfile`; umbrella Helm chart (`anki-mcp-helm-charts`) + ArgoCD app-of-apps tier entry (`argocd/root-chart/values/tier7-services.yaml`); secrets via **Bitwarden → Vault → External-Secrets** (`create-secrets.sh <env>`); dev/prod split via `values-{dev,prod}.yaml` (infra `media-library-b2-provisioning.md`) | §12 |

> ⚠️ **This service breaks new ground in two ways** the other services never touch:
> 1. **It is the first component that talks to the Kubernetes API** — a codebase-wide search
>    finds **no `@kubernetes/client-node` usage anywhere** in `anki-mcp-saas` today. New
>    RBAC, ServiceAccount, and a k8s client seam are all net-new (§9).
> 2. **It provisions per-user B2 keys** — the media-library model is **one bucket-scoped key
>    per environment, held only by the service, with per-user *paths*** (the key never
>    leaves the service). Here the key lives **inside the user's pod**, so a per-user
>    *key* restricted by `namePrefix` is mandatory (ARCHITECTURE §10 layer 2). This service
>    reuses the bucket/region/Bitwarden conventions but adds a **key-management capability**
>    the media-library service deliberately never had (§6.2).

---

## 4. Data model (own Postgres DB)

The service is **stateful** (unlike the CR being the source of truth for *desired workload*,
this service needs durable state the cluster does not hold): the **B2 key handle** must
survive restarts and cannot be re-derived. Follow
the media-library DB conventions (§3): own DB `anki_lifecycle`, Kysely, migrations in a
migrate init-container.

Proposed tables (names illustrative):

- **`instance`** — one row per user. `keycloak_id` (uuid, unique), `instance_name` (the CR
  `metadata.name` = the keycloakId itself; children are `anki-<keycloakId>`, see §5.2 /
  [contracts.md §3](./contracts.md)), `b2_prefix`, `created_at`, `suspended` (bool, mirrors the CR
  power gate `spec.suspended` §5.4), `provisioning_state` (§7 saga:
  `PENDING|B2_READY|SECRET_READY|CR_READY|ACTIVE|DELETING|FAILED`), `deleted_at` (soft-delete
  tombstone for the destructive flow §5.3).
- ~~**`credential`** — the AnkiWeb credential envelope state per user~~ — **DROPPED
  (2026-07)** with the credential channel: no serial, no hkey fingerprint, no endpoint/username
  state. There is nothing to track — AnkiWeb login is user-managed in the pod (VNC) and never
  transits this service.
- **`b2_key`** — per-user B2 application key handle. `keycloak_id`, `b2_key_id` (the
  `applicationKeyId`, **not** the secret), `name_prefix`, `capabilities`, `created_at`,
  `revoked_at`. The `applicationKey` secret itself is **never stored here** — it is written
  straight into the Secret and forgotten (shown once by B2, §6.2).

---

## 5. API surface

### 5.1 Transport mapping (API-first, then bound to saas conventions)

The task specifies a **versioned REST** surface. The saas convention for a *backend* service
is **NATS request-reply fronted by the BFF**, not a public HTTP API (media-library/knowledge-map
are NATS-only; only `tunnel` and `apps/api` expose HTTP, and `apps/api` is the browser door).
Reconcile as follows — the **logical API below is transport-neutral**; bind it thus:

- **Primary (DECIDED 2026-07-11 — home resolved to `anki-mcp-saas`, so this IS the binding):**
  expose it as a NATS RPC service
  (`@nats-kit/nestjs` `subscribeWithReconnect`, request-reply via `msg.respond`, concurrent
  dispatch guarded by Postgres row locks — the media-library `*-rpc.server.ts` pattern) with
  two subjects — `anki.rpc.user.request` (queue group `anki-lifecycle`) and
  `anki.rpc.internal.request` (admin) — using the `ServiceRequest` envelope with
  edge-stamped `keycloakId`. The **`apps/api` BFF** adds an `AnkiLifecycleAdapter` +
  oRPC router methods so the dashboard reaches it exactly like media-library/knowledge-map.
  (Confirmed: **no service in the repo uses NestJS URI versioning** — the contract package
  is the versioned surface, so there is no `/v1` path to add on this transport.)
  **API versioning** rides the contract package version + a `v` field in the envelope
  (mirrors ci-buddy's `"v":1`); there is no URI path to version.
  **(2026-07-11)** The **internal subject** also carries service-to-service calls: the tunnel's
  `ensureConnected` (§5.7) rides `anki.rpc.internal.request` (or a dedicated subject in the
  contract package — implementation's call), never the user subject.
- ~~**Alternative (only if it lands standalone with its own ingress):**~~ **(dead for v1 —
  home resolved 2026-07-11 to `anki-mcp-saas`; retained for context)** a NestJS HTTP
  controller under a URI version prefix **`/v1/…`** (`app.setGlobalPrefix` +
  `enableVersioning({ type: URI })`), Keycloak Bearer JWT validated as a resource server
  (reuse the `packages/mcp-auth` verifier pattern). Use this only if the "standalone
  atomic service in anki-mcp-saas" decision (§14) resolves toward a directly-exposed API.

The endpoint table below reads as REST for clarity; each row maps 1:1 to a contract method.

### 5.2 Resource model

`instance` is the resource; there is **exactly one per user**. The CR `metadata.name` **is the
keycloakId** and `spec.user` carries the same value; children are `anki-<keycloakId>`
([contracts.md §3](./contracts.md)). Because the name is the (stable) keycloakId, create is
idempotent and the ~~activator~~ **VNC gateway** (2026-07-11) resolves `user → pod DNS` by the
same naming convention ("routing = naming convention, not stored data"). The caller **never**
supplies an arbitrary instance id; identity is the edge-stamped `keycloakId`.

### 5.3 Endpoints

| Method | Logical REST | Contract method | Effect |
|---|---|---|---|
| Create | `POST /v1/instance` | `instance.create` | Run the provisioning saga (§7): B2 key → Secret → `AnkiInstance` CR create. Idempotent on the user. |
| Get status | `GET /v1/instance` | `instance.status` | Project CR `.status` + this service's `provisioning_state` into the dashboard shape (§10). |
| Stop | `POST /v1/instance/stop` | `instance.stop` | Explicit user "off": set `spec.suspended: true` (§5.4). `replicas` is left as-is — the gate forces the pod to 0 (pod `preStop` owns the graceful drain). |
| Start | `POST /v1/instance/start` | `instance.start` | User "on": set `spec.suspended: false` **and `spec.replicas: 1`** (2026-07-11 — one atomic intent under the single writer; this is also the **manual wake from sleep**, ARCHITECTURE §16.4). ~~The next request wakes it via the activator.~~ |
| Reset | `POST /v1/instance/reset` | `instance.reset` | Recreate the pod, **keep data** (§5.5): bump `spec.restartedAt`. Also the dashboard **Reconnect** lever (§5.7). |
| Delete | `DELETE /v1/instance` | `instance.delete` | Destructive teardown incl. data fate (§5.6). Requires confirmation. |
| ~~AnkiWeb re-login~~ | ~~`POST /v1/instance/ankiweb-login`~~ | ~~`instance.ankiwebLogin`~~ | **REMOVED in v1 (2026-07)** — no credential flow; the user logs into AnkiWeb inside Anki over VNC (§6.1/§6.3 notices). |
| **Internal: ensure-connected** | — (NATS internal subject only) | `internal.ensureConnected` | **(added 2026-07-11)** Tunnel-called wake (§5.7): checks instance/tier/gate; asleep → `replicas: 1`; running-but-disconnected → bump `restartedAt`; typed rejection otherwise. |
| **Admin: list** | `GET /v1/admin/instances` | `admin.list` (internal subject) | List all instances + phases. Admin only (§8). |
| **Admin: force-stop** | `POST /v1/admin/instance/:user/stop` | `admin.forceStop` | Set the power-gate on any user's instance. Admin only. |

### 5.4 Stop / Start — the `spec.suspended` power gate

> **2026-07-11 update:** with the activator shelved, this service is the only writer of BOTH
> fields — the two-writer race rationale below is **historical**. The gate itself is unchanged
> and still load-bearing: `suspended` = **off** (power gate), `replicas` = **sleep/wake**; the
> operator still computes `effectiveReplicas = suspended ? 0 : replicas`. The dashboard renders
> the three states off / sleep / on (ARCHITECTURE §16.4).

**Problem (historical framing).** ARCHITECTURE was explicit: the **activator** was the only
writer of `spec.replicas` (§7, decision 7), and the lifecycle service "writes/patches CRs only"
(§8). But an explicit user **Stop** must keep the instance *off* even though traffic would
otherwise wake it — that is a control-plane intent, not an idle-TTL event.

**Resolution (decided — [contracts.md §2](./contracts.md)).** The power gate is a **separate CR
field, `spec.suspended` (bool)**, distinct from `spec.replicas`:

```yaml
spec:
  user: 3f9b2c1a-…      # keycloakId (= metadata.name)
  replicas: 0          # 0↔1 wake/sleep WITHIN the gate (2026-07-11: lifecycle-owned; was activator's)
  suspended: false     # lifecycle-owned power gate (true = user/admin "off")
```

- **Lifecycle service** writes `spec.suspended` only. `true` = user pressed off / admin
  force-stop.
- **Operator** computes `effectiveReplicas = suspended ? 0 : replicas` and scales the
  StatefulSet to 0 while suspended, regardless of `replicas`.
- ~~**Activator** honors the gate: when `suspended: true` it never waits for a wake … (requirements-activator §6.2).~~
  **(shelved 2026-07-11)** The equivalent check now lives in THIS service's `ensureConnected`
  (§5.7): when `suspended: true` it refuses to wake, and the tunnel rejects the MCP request with
  a typed "instance stopped" reason.
- Two intents, **two disjoint fields** → off (gate) and sleep (replicas) stay distinguishable for
  the dashboard; under the historical two-writer contract this also prevented a write race.

**Rejected alternative (historical):** lifecycle service patches `spec.replicas: 0` directly for
an explicit stop. Rejected — under the two-writer contract it raced the activator's next wake
patch; even under single-writer it would conflate "off" with "asleep", which the dashboard must
render as different states.

### 5.5 Reset — recreate pod, keep data

Reset = "my Anki is wedged, restart it without losing anything." Since the platform forbids
live pod surgery (§2 rule 3), implement it as **desired-state churn the operator reconciles**.
**RESOLVED (2026-07-11 — ARCHITECTURE §16.6):** the field is **`spec.restartedAt`** (RFC3339
timestamp, written by this service); the operator copies the value into the StatefulSet pod
template as an **annotation** (the `kubectl rollout restart` pattern) → pod recreated while the
PVC (`whenScaled: Retain`, `whenDeleted: Retain`) and B2 data are untouched (ARCHITECTURE §3).
The lifecycle service **does not** delete the pod itself. Reset **must not** rotate credentials
or the B2 key (data-preserving by definition).

Besides user-initiated reset, this service bumps `spec.restartedAt` for:
- **tunnel reconnect / takeover** (§5.7 — running-but-disconnected lever; dashboard Reconnect).

(~~hkey pickup after AnkiWeb re-login~~ — removed 2026-07 with the credential channel; a VNC
login needs no pickup, it lands directly in the running pod's profile.)

### 5.6 Delete — destructive flow (data fate spelled out)

Delete is the one irreversible operation; treat it like `rm -rf` on the user's world.

**Confirmation (required).** The mutation is **two-phase**:
1. `instance.delete` with **no** confirmation token → returns a `confirmationRequired`
   response describing exactly what will be destroyed (PVC/SQLite collection, the B2 media
   prefix, the Secret, the CR) and a short-lived, single-use `confirmationToken` bound to
   the user + a data-fingerprint (e.g. media object count / bytes) so a stale confirm can't
   ride a changed instance.
2. `instance.delete` **with** the token (and, for the dashboard, a typed-name check à la
   GitHub repo delete) → executes.

**Data fate — must be an explicit, logged choice, default = destroy everything:**

| Store | Default on delete | Mechanism |
|---|---|---|
| **AnkiInstance CR** | deleted | delete the CR; operator garbage-collects owned objects |
| **PVC (SQLite collection, prefs, addons)** | **deleted** | **RESOLVED (2026-07-11 — ARCHITECTURE §16.7):** operator-owned via a **CR data-fate field** (e.g. `spec.dataRetention: Delete\|Retain`; the operator spec owns exact naming). This service sets the field, then deletes the CR; the operator deletes the PVC in its **finalizer flow** when fate=Delete. This service never touches the PVC (zero workload-object RBAC verbs, §9). The **`retainData` option** maps to `Retain` (account-hold / accidental-delete safety). |
| **B2 per-user media prefix** | **deleted** | best-effort delete of all objects under the user's prefix (bounded-concurrency `DeleteObject`, per the media-library seam), then **revoke the B2 key** (§6.2). Offer `retainData` to skip. |
| **B2 application key** | **revoked always** | `b2_delete_key`; a leaked key must die even when data is retained. |
| **k8s Secrets** | deleted always | **both** `anki-<keycloakId>` (tunnel token) and `anki-<keycloakId>-b2` (B2 env creds). (The user's AnkiWeb login lives in `prefs21.db` on the PVC and dies with the PVC per the data-fate field.) |
| **DB rows** | soft-delete tombstone, then GC after a grace window | audit + late-arriving webhook safety. |

Delete is a **saga** with the same idempotency/partial-failure discipline as create (§7):
order it **set data-fate field → delete CR (operator finalizer tears down pod + PVC per fate) →
B2 objects → B2 key revoke → Secrets (both) → DB tombstone**, each step idempotent and
retryable, so a mid-delete crash resumes cleanly. Revoking the B2 key
and deleting **both** Secrets are the **security-critical** steps and must be retried until they
succeed (surface a `FAILED` state that keeps retrying, never silently give up on a live key).

### 5.7 Wake / ensure-connected — the tunnel-facing NATS method (added 2026-07-11)

**DECIDED 2026-07-11 (ARCHITECTURE §16.5).** When an MCP request arrives at the tunnel and no
client is connected for that user, the tunnel makes **ONE NATS call** to this service — "ensure
user X's instance is connected" (`internal.ensureConnected`, internal subject, §5.1). This
service does **all** checks and picks the lever; the pod is untrusted and carries no
tier/entitlement data (contracts.md §8c):

1. **Checks (server-side, fail closed):** instance exists (no row/CR → typed `NO_INSTANCE` —
   **never auto-create**); tier still allows hosting (§5.9); power gate off (`suspended: true` →
   typed `INSTANCE_STOPPED`). **AnkiWeb login is NOT required** for MCP — it affects sync only.
2. **Lever:** asleep (`replicas: 0`) → set `spec.replicas: 1`; running but add-on disconnected
   (kicked / dropped WS — the hosted add-on never auto-reconnects) → bump `spec.restartedAt`
   (§5.5) so the fresh pod's add-on connects on startup.
3. **The hold lives in the tunnel, not here:** the tunnel holds the MCP request until the add-on
   connects (~60–90s timeout) and then forwards; on a failed check it rejects with the typed
   reason. This method returns promptly after the CR write — it never blocks on pod readiness.

One mechanism covers the sleeping pod, the kicked pod, and the dashboard **Reconnect** button
(dashboard → api → `instance.reset` → `restartedAt` bump; ~30–60s accepted for v1). "Last client
connected wins" (existing tunnel kick behavior) is unchanged; because all hosted disconnects are
terminal, the pod can never fight a local client.

### 5.8 Idle TTL & downscale trigger (moved from the activator, added 2026-07-11)

**DECIDED 2026-07-11 (ARCHITECTURE §16.2).** This service owns the idle TTL: track per-user
last-activity from (a) the tunnel's existing **per-request quota events** and (b) the **VNC
gateway's session-activity signals**; on TTL expiry set `spec.replicas: 0`. Setting replicas to 0
is sufficient for a graceful downscale — the pod's `preStop` hooks own the quit/drain sequence
(contracts.md §10); this service never waits on the drain. Timestamp storage (in-memory vs a DB
column) is an implementation detail; losing it on a restart costs at most one fresh TTL window
per warm pod (the same accepted worst case the activator design carried).

### 5.9 Tier gate & lapse — the `pro` tier (added 2026-07-11)

**DECIDED 2026-07-11 (user).** A new tier **`pro`** gates hosted instances (existing tiers stay
as-is):

- **Create is `pro`-gated and explicit:** `instance.create` requires the `pro` tier and an
  explicit dashboard create button — **no auto-create** on upgrade, and never on MCP traffic
  (§5.7 check 1).
- **Lapse (downgrade/expiry):** this service consumes the Users service's
  **`subscription.tier_changed`** JetStream events. On lapse: **power gate on immediately**
  (`spec.suspended: true` — the user cannot start the instance until they resubscribe), then a
  **1-month grace period**, then the **full delete saga** (§5.6: PVC + B2 prefix + B2 key revoke
  + both Secrets + CR). Re-upgrading within grace just clears the gate; nothing is rebuilt.

---

## 6. Credential flows

### 6.1 AnkiWeb hkey mint

> **REMOVED in v1 (2026-07, platform decision — see the top banner).** This flow is not
> implemented: there is no `ankiwebLogin` trigger and the service never sees an AnkiWeb
> password or hkey. The user logs into AnkiWeb inside Anki over VNC; the login persists on
> the PVC. The verified wire-shape research below is retained as protocol knowledge only
> (it is the same exchange desktop Anki performs internally on login).

**Trigger:** ~~`instance.ankiwebLogin`~~ (none in v1 — see notice above).

**Input:** `{ username, password, endpoint? }`. `password` is **request-scoped, in-memory,
never logged, never persisted, never written to the CR or DB** (§2 rule 4). Redact it from
any error/trace.

**Exchange — verified wire shape (RESOLVED 2026-07-11, verified against Anki source at tag
25.09.2 — implement DIRECTLY in Node, no shelling to Anki):**

```
POST {endpoint}sync/hostKey                  # default endpoint: https://sync.ankiweb.net/
headers:
  anki-sync: {"v":11,"k":"","c":"<client-ver>","s":"<random>"}
  content-type: application/octet-stream
body:      zstd( JSON {"u": <username>, "p": <password>} )
response:  zstd( JSON {"key": "<hkey>"} )    # carries an anki-original-size header
403 ⇒ bad credentials
```

- **Protocol v11**, stable since 2023; the server accepts the range **[8,11]** — **hardcode 11**.
- zstd via **`node:zlib`** (available since Node ≥ 22.15) — no native dependency.
- **Redirects:** AnkiWeb's 308 sharding redirects apply to *later sync calls only*, not to the
  login/hostKey call.
- **Rate limits:** AnkiWeb's server-side limits are **undocumented** — mint once per credential
  change, back off on failures, and **hard-stop on repeated 403** (never retry-loop; this service
  must not become a password oracle against AnkiWeb — see also §7 rate limiting).
- (The former alternative — shelling out to Anki's own backend `sync_login` — is **dropped**;
  there is only one mode, so `ANKIWEB_SYNC_LOGIN_MODE` is removed from §11.)
- **Threat sizing (still true, now for the PVC):** the hkey is **account-scoped, not
  per-device**; there is no granular AnkiWeb revoke — the only kill switch is the user
  changing their AnkiWeb password. A leaked hkey = full read/write on the collection until
  then. Post-removal it lives only in `prefs21.db` on the PVC (after the user's VNC login) —
  treat that volume as secret-grade.

~~**On success:** write the Secret (§6.3) with `serial = last_serial + 1`, store the
fingerprint envelope, drop the password. **Endpoint allowlist** on custom endpoints.~~
(All removed with the flow — see the notice at the top of §6.1.)

### 6.2 B2 per-user application key

**Model (diverges from media-library — see §3 warning).** Each user gets their **own** B2
application key **restricted to the shared bucket + a per-user `namePrefix`** so a leaked
pod key yields only that user's media (ARCHITECTURE §10 layer 2; reuse — do not reinvent —
the `media-library-b2-provisioning.md` bucket-scoped + per-user-prefix model, extended from
*paths* to *keys*).

- **Prefix:** deterministic per user, e.g. `anki/<keycloakId>/…` (analogous to media-library's
  `media/<ownerId>/…` in `path.util.ts::objectKeyFor`). The user's `collection.media/` and
  `media.trash/` rclone mount targets live under this prefix (ARCHITECTURE §4).
- **Capabilities (RESOLVED 2026-07-11):** minimal set for rclone read/write/list/delete under the
  prefix (`listFiles`/`readFiles`/`writeFiles`/`deleteFiles`), `namePrefix`-restricted to the
  user's prefix, `bucketId`-restricted to the anki bucket — the restrictions **compose** as
  expected. The key **MUST additionally carry `listAllBucketNames`**: S3 clients (incl. rclone)
  may call ListBuckets at init and **fail on bucket-restricted keys without it**.
- **Provisioning identity:** the lifecycle service holds a **B2 key-manager key** with
  `writeKeys`+`deleteKeys` (via the Bitwarden→Vault→ESO pipeline, per env, like the
  media-library key). It calls `b2_create_key` to mint the per-user key. This is the extra
  privilege the media-library service intentionally never had. ⚠️ **(verified 2026-07-11)** a
  `writeKeys` credential is **MASTER-EQUIVALENT** per Backblaze docs — no downscoped
  key-minting exists — making it the platform's highest-value secret: backend-only, never near a
  pod. Restriction inheritance for keys-created-by-restricted-keys is **undocumented — do not
  rely on it**; mint every per-user key with the key-manager credential directly.
- **The `applicationKey` secret is shown once** by B2 — capture it, write it straight into
  the Secret / the location the CSI/pod expects (§6.3), and **never store it** (only the
  `applicationKeyId` handle goes in `b2_key`, §4).
- **Rotation:** re-mint (create new, write, then delete old) on demand / on a schedule; the
  pod must pick up the new B2 key — coordinate delivery with the FUSE/CSI design (§15).
- **Revocation:** `b2_delete_key` on instance delete (always, §5.6) and on rotation (old key).
- ~~⚠️ **Verify B2 account limits (§15).**~~ **RESOLVED (2026-07-11, verified):** key-per-user is
  **viable** — the documented cap is **100M application keys per account**, far beyond target
  scale. The real limit is throttling: **queue key create/delete with backoff** (429/503; key
  operations are Class C transactions) rather than issuing them inline in a burst.

**Where the B2 key is delivered (DECIDED 2026-07-10, [contracts.md §8b/§9](./contracts.md)).**
The lifecycle service writes a **SECOND, SEPARATE per-user Secret** named
**`anki-<keycloakId>-b2`** (namespace `anki-instances`), holding ONLY the rclone/B2 **env**
creds. The operator `envFrom`s this Secret into the **rclone sidecar only**. It is a distinct
object from the credentials Secret (`anki-<keycloakId>`, §6.3) **on purpose**: the credentials
Secret holds the tunnel token and its `tunnel-credentials.json` key **is** a valid env-var name
(the kubelet does **not** skip it), so folding the B2 creds into it and `envFrom`ing the whole
thing would leak the token into the privileged sidecar's environment. Keep them apart.

**`anki-<keycloakId>-b2` required keys** (the `s3`-at-B2 backend set; matches
`rclone-mount.sh`, which builds `b2:${B2_BUCKET}/${B2_PREFIX}` and dies on empty `B2_BUCKET`):

| Key | Required | Notes |
|---|---|---|
| `RCLONE_CONFIG_B2_TYPE` | yes | e.g. `s3` (rclone remote name is `b2`) |
| `RCLONE_CONFIG_B2_PROVIDER` | yes | the S3 provider identifier for the B2 endpoint |
| `RCLONE_CONFIG_B2_ACCESS_KEY_ID` | yes | the per-user B2 `applicationKeyId` |
| `RCLONE_CONFIG_B2_SECRET_ACCESS_KEY` | yes | the per-user B2 `applicationKey` (shown once) |
| `RCLONE_CONFIG_B2_ENDPOINT` | yes | e.g. `s3.eu-central-003.backblazeb2.com` |
| `B2_BUCKET` | yes | the shared anki bucket name |
| `B2_PREFIX` | optional | the per-user prefix `anki/<keycloakId>` |

If the still-open backend choice (contracts.md decision #5) lands on native `b2`, the
`RCLONE_CONFIG_B2_*` value set changes (`_ACCOUNT`/`_KEY` in place of the S3 access-key pair);
`envFrom` is backend-agnostic. **Both** Secrets are this service's write surface, and both are
deleted on instance delete (§5.6).

### 6.3 The credentials Secret this service writes

> **Sync-credentials payload REMOVED in v1 (2026-07 — see the top banner and
> [contracts.md §8](./contracts.md)).** This Secret formerly carried the ci-buddy hkey JSON
> (`sync-credentials.json`, monotonic `serial`, endpoint, username) plus the restart-based /
> file-poll pickup machinery — all gone. There is no serial, no rotation UX, no
> "credentials active within a minute", and no restart-for-pickup.

The lifecycle service still writes the **Secret object** **`anki-<keycloakId>`** (namespace
`anki-instances`), now carrying **only** the `tunnel-credentials.json` data key (§6.4,
[contracts.md §8c](./contracts.md)). The operator owns the object→file mount (whole-volume at
`/run/ankimcp`, never `subPath`). Perms/ownership of the mounted file (`0400`, Anki uid) are
the operator's `defaultMode`/`fsGroup` job; the lifecycle service only guarantees the object
contents. B2 key material does **NOT** ride this Secret — it goes in the separate
`anki-<keycloakId>-b2` Secret (§6.2, contracts.md §8b) so the token never reaches the
privileged sidecar's env.

### 6.4 Pod→tunnel token — the Secret data key (PINNED 2026-07-11, contracts.md §8c)

This service mints an **opaque per-instance bearer token** and writes it as the data key
`tunnel-credentials.json` in the credentials Secret `anki-<keycloakId>` (its **only** data key
since the 2026-07 sync-credentials removal, §6.3) — whole-volume
mount, anki container only, never `envFrom`'d (§6.2's separation reasoning applies
identically). It authenticates the pod's AnkiMCP add-on to the tunnel and marks the connection as
**hosted**.

- **Shape and rules are pinned in [contracts.md §8c](./contracts.md):** `{"v":1,"token":…,
  "tunnelUrl":"wss://<internal-tunnel-host>/connect","user":{"id":"<keycloakId>"}}`; `v` strict=1;
  the add-on presents `token` verbatim as `Authorization: Bearer` on its WS upgrade; this service
  **ALWAYS writes `tunnelUrl`** (env-specific in-cluster host); `user.id` is display-only; **NO
  tier/entitlement data in the file** (pod untrusted — checks are server-side, §5.7).
- **The tunnel validates the token against the lifecycle signing key** (config, §11) — key
  distribution rides the normal Bitwarden→Vault→ESO pipeline.
- Mint fresh, **never persist the token** (a fingerprint for audit is fine); it never touches
  the PVC.
- **Rotation = replace the file + restart the pod** (`spec.restartedAt`).
- Written during the create saga (§7 step 2); it is **always** present (MCP works without an
  AnkiWeb login).

---

## 7. Provisioning orchestration

Signup and delete are **sagas** with durable state (`instance.provisioning_state`, §4),
because they span three external systems (B2, k8s Secrets, k8s CRs) that fail independently.

**Create order (dependency-first):** `B2 key → Secret → AnkiInstance CR`.

1. **B2 key** (`b2_create_key`, §6.2) → persist `b2_key` row, state `B2_READY`.
2. **Secrets — write BOTH** (state `SECRET_READY`): (a) the credentials Secret
   `anki-<keycloakId>` with its single data key `tunnel-credentials.json` (the tunnel token,
   §6.4 — always written; the former `sync-credentials.json` hkey key was removed in v1, §6.3)
   and (b) the separate `anki-<keycloakId>-b2` Secret with the B2 key material (§6.2). Keep the
   B2 creds OUT of the credentials Secret so the token never reaches the sidecar env.
3. **AnkiInstance CR** create (server-side apply, ARCHITECTURE §12 item 11 —
   `ServerSideApply=true` because ArgoCD sync-waves are non-functional) → state `CR_READY`,
   then `ACTIVE` when the CR status reports provisioned.

**Idempotency (mandatory).** Instance name is deterministic (§5.2) and every step is
idempotent: `b2_create_key` guarded by "does this user already have a live key?", Secret via
apply (create-or-update), CR via server-side apply. Re-running `instance.create` for a user
mid-saga **resumes** from `provisioning_state`, never double-provisions. A duplicate request
returns the current state, not an error.

**Partial-failure recovery.**
- **Forward-recovery first:** a background reconciler re-drives any row not in a terminal
  state (`ACTIVE`/deleted) toward its goal — retry the failed step with backoff.
- **Compensation on hard-fail:** if create wedges unrecoverably, roll back in reverse
  (delete CR → delete both Secrets → **revoke B2 key**) so no orphaned live key or Secret lingers.
- **Crash safety:** because state is in Postgres and every step is idempotent, a crash at any
  point is resumed by the reconciler on restart. No step depends on in-memory progress.

**Rate limiting.** Cap expensive/abusable operations per user and globally:
- **AnkiWeb login** attempts (protects the user's AnkiWeb account from lockout and the
  service from being used as a password oracle) — per-user throttle + backoff on repeated
  auth failures.
- **B2 key create/rotate** (B2 key quota is finite, §6.2) — per-user + global ceiling.
- **create/delete/reset** — modest per-user rate to stop churn storms.
Reuse the platform throttling approach (tunnel uses `ThrottlerGuard` with `trust proxy`;
`apps/tunnel/src/main.ts`, `apps/tunnel/src/friction/*`).

---

## 8. AuthN / AuthZ

**Reuse the saas auth stack — do not invent one.** Identity is **Keycloak**. Two paths,
matching the convention:

- **Via the BFF (primary):** the dashboard authenticates to `apps/api` (Keycloak PKCE
  session); the BFF stamps the authenticated **`keycloakId`** onto the `ServiceRequest`
  envelope and forwards to the **user subject** `anki.rpc.user.request`. Identity is
  **edge-stamped, never caller-supplied** (media-library subjects convention). The lifecycle
  service trusts the envelope `keycloakId` exactly as media-library/knowledge-map do.
- **Standalone HTTP (alternative):** validate a Keycloak **Bearer JWT** as a resource server
  (issuer + JWKS + audience), reusing the `packages/mcp-auth` verifier pattern
  (`mcp-token-verifier.ts`, `mcp-auth.guard.ts`).

**Authorization rules:**
1. **A user may only manage their own instance.** Every user-facing op scopes strictly by the
   edge-stamped `keycloakId`; the caller cannot name another user's instance (the resource is
   implicit, §5.2). This is the same "scope everything by keycloakId" rail media-library
   relies on.
2. **Admin surface is a separate namespace.** `admin.*` methods ride the **internal subject**
   `anki.rpc.internal.request` — the BFF **never** forwards user traffic there (subjects
   convention). Admin identity is gated on the Keycloak **`ADMIN` role** (`UserRole` enum,
   `packages/shared-types/src/roles.ts`; resolved via `@repo/users-client`). `admin.list` /
   `admin.forceStop` operate across users by explicit user id and are audit-logged.
3. **No ambient authority:** the service's own k8s/B2 privileges (§9, §6.2) are never
   exposed through the API — a user op can only produce the CR/Secret writes its own identity
   authorizes.

---

## 9. Kubernetes access

**Runs in-cluster** with a dedicated ServiceAccount. **This is the first component in the
platform to hold k8s API credentials** (§3 warning) — scope them to the minimum.

- **Client:** a single k8s client seam (e.g. `@kubernetes/client-node`) analogous to the S3
  seam — the rest of the service speaks `createInstance/patchDesiredState/writeSecret` in
  domain terms and never imports the k8s SDK directly (mirrors
  `object-storage.service.ts`).
- **RBAC (namespaced Role in the anki namespace, not ClusterRole):**
  - `ankiinstances.<group>` (the CRD): `get, list, watch, create, update, patch, delete`.
  - `secrets`: `get, list, watch, create, update, patch, delete` — **scoped as tightly as
    the API allows** (Secrets can't be `resourceNames`-restricted for `create`, so this Role
    lives in a namespace that holds **only** anki per-user Secrets; document that isolation).
  - **Nothing else.** No StatefulSets, Pods, PVCs — no workload-object verbs at all.
    ~~(PVC delete for the destructive flow, §5.6, was the one candidate exception.)~~
    **RESOLVED (2026-07-11 — ARCHITECTURE §16.7):** PVC deletion **is a CR data-fate field the
    operator honors** (§5.6), so this Role stays **CRs + Secrets only**.
- **No cluster-admin, no cross-namespace reach.** The service must not be able to touch
  `pg-shared`, Vault, Keycloak, etc. — same crown-jewels list the pod NetworkPolicy denies
  (ARCHITECTURE §10 layer 1). Its own DB/B2/Keycloak reach is via normal service creds, not
  the k8s SA.
- **Server-side apply** for CR writes (`ServerSideApply=true`, ARCHITECTURE §12 item 11) so
  it tolerates racing the operator/CRD install during ArgoCD's all-at-once sync.
- **Watch** the CR `.status` to project it for the dashboard (§10) rather than poll.

---

## 10. Status & events for the dashboard

The dashboard (ARCHITECTURE §9) needs a projected, stable view — never raw CR internals.

- **`instance.status`** maps CR `.status.phase` + this service's `provisioning_state` into the
  **canonical status enum pinned in [contracts.md §11](./contracts.md)** (2026-07-11 — unified
  with the dashboard's `InstanceStatus`):
  `None | Provisioning | Stopped | Suspended | Starting | Running | Stopping | Deleting | Error`.
  ~~`Provisioning | Stopped | Suspended (idle) | Waking | Running | Resetting | Deleting |
  Failed`~~ (2026-07-11: `Waking` → `Starting` and `Resetting` → `Starting` — `Starting` covers
  wake AND restart-in-progress; `Failed` → `Error`; `Deleting` kept). (`Suspended` = replicas 0
  by idle TTL, from the ~~activator's~~ idle-TTL (§5.8) view; `Stopped` = the explicit power-gate
  §5.4 — distinguish them, they mean different things to the user.) **(2026-07-11)** The dashboard renders these as
  the three user-facing power states (ARCHITECTURE §16.4): `Stopped` → **off**, `Suspended` →
  **sleep**, `Running` → **on**; transitional statuses render as spinners. The projection SHOULD
  also expose the **tunnel-connection state** source for the panel's connected/disconnected
  indicator — in practice the dashboard reuses the existing tunnel-status seam rather than this
  service re-projecting it.
- ~~**Credential signal (decision 10)**~~ — **DROPPED (2026-07)** with the credential channel:
  no `ankiwebLogin`, no serial, no "credentials active within a minute" string, no
  `lastCredentialSerial`/`credentialUpdatedAt` fields.
- **Live events:** publish a per-user event on every mutation the dashboard cares about
  (created, stopped/started, reset, deleting, failed), following the
  media-library event convention (`<domain>.<keycloakId>.updated` core-NATS fire-and-forget;
  `mediaLibraryEventSubject`), which the BFF fans out to the browser over its WS.
- **AGPL source-link data (ARCHITECTURE §15):** expose the shipped Anki build + baked add-on
  versions / the "corresponding source" URL so the dashboard footer / noVNC page can render
  the required AGPL §13 source-offer link. This is read-only metadata (from the CR/image
  labels or config) — the service just surfaces it.

---

## 11. Config surface

Follow the media-library `configuration.ts` fail-fast pattern (§3): `validateEnv()` at boot
lists **every** missing var. Required groups:

```
# Server / health
PORT, NODE_ENV, LOG_LEVEL, LOG_FORMAT

# Own Postgres (assembled by @repo/config getDatabaseConfig)
DATABASE_HOST, DATABASE_PORT, DATABASE_NAME, DATABASE_USER, DATABASE_PASSWORD, DATABASE_SSL

# NATS (getNatsConfig) — for the RPC service + event publish
NATS_URL / NATS_* per @repo/config

# Keycloak (resource-server validation, if standalone HTTP)
KEYCLOAK_ISSUER_URL / JWKS / AUDIENCE  (reuse mcp-auth config keys)

# Users service (role/tier lookup)
USERS_SERVICE_URL, USERS_SERVICE_SECRET

# Kubernetes
ANKI_NAMESPACE                 # anki-instances — holds AnkiInstance CRs + per-user Secrets (contracts.md §4)
ANKIINSTANCE_GROUP/VERSION     # anki.ankimcp.ai / v1alpha1 (contracts.md §1; or hardcode in the client seam)
# in-cluster SA token is mounted, not an env var

# AnkiWeb — no vars (2026-07: the credential flow is removed, §6.1)
# ANKIWEB_ENDPOINT_ALLOWLIST — DROPPED 2026-07 with the credential flow
# ANKIWEB_SYNC_LOGIN_MODE — DROPPED 2026-07-11: only one mode existed (direct hostKey HTTP, §6.1)

# Tunnel (pod→tunnel token §6.4; wake/idle §5.7–§5.8) — names illustrative
ANKI_TUNNEL_WS_URL             # env-specific internal wss://…/connect written into tunnel-credentials.json
TUNNEL_TOKEN_SIGNING_KEY       # the lifecycle signing key the tunnel validates hosted tokens against
IDLE_TTL, IDLE_SWEEP_INTERVAL  # idle-sleep knobs (§5.8)

# B2 (per-user key provisioning)
OBJECT_STORAGE_ENDPOINT, OBJECT_STORAGE_REGION, OBJECT_STORAGE_BUCKET
B2_KEY_MANAGER_KEY_ID, B2_KEY_MANAGER_APPLICATION_KEY   # the writeKeys/deleteKeys key
ANKI_B2_PREFIX_TEMPLATE        # e.g. anki/{keycloakId}

# Rate limits (positive ints, no silent defaults — media-library style)
LOGIN_RATE_*, B2_KEY_RATE_*, LIFECYCLE_MUTATION_RATE_*

# OpenTelemetry (required unless OTEL_ENABLED=false)
OTEL_ENABLED, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES
```

Secrets (`DATABASE_PASSWORD`, `USERS_SERVICE_SECRET`, `B2_KEY_MANAGER_APPLICATION_KEY`,
`TUNNEL_TOKEN_SIGNING_KEY`, Keycloak client secret) come via **Bitwarden → Vault →
External-Secrets**, per-env
(`create-secrets.sh <env>`, infra convention). Non-secret endpoint/region/bucket/namespace
live in `values-{dev,prod}.yaml`.

---

## 12. Observability, shutdown, multi-env, deployment

- **Observability:** `instrumentation.ts` imported **first** in `main.ts` (OTLP HTTP traces
  + Prometheus pull on a unique `OTEL_PROMETHEUS_PORT`); Pino via `@repo/logger` with OTel
  trace-context mixin; `@repo/health` `HealthModule.forRoot` exposing `/health/live|ready|
  full` with `PostgresCheck` + `NatsCheck` as the **critical** checks (media-library
  pattern). **Match the convention: object storage / Keycloak / users are deliberately NOT
  readiness deps** — they degrade **per-request** rather than evict the pod, so a B2 or
  Keycloak blip doesn't take the control plane down. The **k8s API** reachability is the one
  judgement call: prefer per-request degradation too (a k8s-API blip shouldn't NACK the pod),
  surfacing it as a `Failed`/retrying provisioning state rather than a readiness failure.
  **Redaction:** never log the B2 `applicationKey` or the tunnel token — log
  fingerprints only (error logs use `{ err: error }` per repo convention). (The AnkiWeb
  password/hkey no longer transit this service at all — 2026-07 banner.)
- **Shutdown:** `OnApplicationShutdown` flushing telemetry last; NATS RPC loop + Kysely pool
  + S3/k8s clients closed in their own `OnModuleDestroy` (knowledge-map shutdown pattern). A
  saga in flight must be safe to interrupt (state is in Postgres, §7).
- **Multi-env:** dev/prod via `values-{dev,prod}.yaml`; **each env has its own B2 bucket +
  key-manager key** (media-library provisioning doc — per-env Bitwarden item). Dev
  integration uses a **B2 sandbox bucket** (or MinIO where B2-specific key APIs aren't
  needed) and envtest/kind for k8s (§13).
- **Deployment:** per-service multi-stage `Dockerfile` (`turbo prune` template, non-root,
  `node --require ./dist/instrumentation.js dist/main.js`); **migrate init-container** running
  `src/database/migrate.ts` with a **unique per-service migration table name** (e.g.
  `anki_lifecycle_kysely_migration` — collision-avoidance, media-library pattern). **CI is
  Jenkins → Harbor** (`harbor.anatoly.dev/ankimcp`), versioned via **changesets** (never
  hand-edit `package.json` version) — add the service to the root `Jenkinsfile` (APP choice,
  version line, build-and-push stage). **Helm charts and ArgoCD apps live in SEPARATE repos**
  (`anki-mcp-helm-charts` + `anki-mcp-infrastructure` `argocd/apps/…` app-of-apps) — none of
  it is in this/the saas repo. **New wiring to add there (ARCHITECTURE §13):** the
  ServiceAccount + namespaced Role/RoleBinding (§9), the CRD in an **early tier** (must exist
  before this service), the B2 key-manager Bitwarden item per env, and a new DB
  `anki_lifecycle` on `pg-shared` (CNPG database + role templates, like
  `database-media-library.yaml`), plus the Prometheus scrape port.

---

## 13. Testing

**Unit (pure functions, no k8s/NATS — media-library keeps logic `aqt`/infra-free for exactly
this):**
1. ~~**Serial monotonicity**~~ — dropped (2026-07): no credential serial exists.
2. **Idempotency:** repeated `instance.create` for a user produces one B2 key, one Secret,
   one CR; resumes from each `provisioning_state`.
3. **AuthZ:** a user request scoped to keycloak id A can never resolve/mutate B's instance;
   `admin.*` rejects non-ADMIN; user subject never reaches an admin method.
4. **Delete confirmation:** delete without/with-stale token refused; data-fate matrix (§5.6)
   honored incl. `retainData`.
5. **Secret payload:** the `tunnel-credentials.json` key exactly matches contracts.md §8c
   (`v:1/token/tunnelUrl/user.id`; `tunnelUrl` always present; no tier data); no other data
   key exists (§6.3).
6. **Redaction:** B2 key/tunnel token never appear in logs or traces (fingerprint only).

**Added 2026-07-11:**

- **ensureConnected levers (§5.7):** asleep → `replicas: 1`; running-but-disconnected →
  `restartedAt` bump; suspended / tier-lapsed / no-instance → typed rejection with **zero CR
  writes** (fail-closed, no auto-create).
- **Idle TTL (§5.8):** activity signals reset the clock; expiry writes `replicas: 0` exactly
  once; an active signal mid-window prevents the downscale.
- **Tier lapse (§5.9):** `subscription.tier_changed` (lapse) → `suspended: true` immediately;
  grace expiry → delete saga; re-upgrade within grace clears the gate and cancels the saga.
- **Restart-based pickup (§6.3):** re-login with the pod running bumps `restartedAt`; with the
  pod asleep it does not.

**Integration:**
8. **k8s against envtest / kind:** create/patch/delete `AnkiInstance` CRs and Secrets;
   assert server-side apply, the power-gate field write (§5.4) leaves `spec.replicas`
   untouched, reset churns the restart field without touching data fields, delete GCs
   correctly. Assert RBAC is actually minimal (a StatefulSet write is *denied*).
9. **B2 against a sandbox bucket:** `b2_create_key` with `namePrefix`+`bucketId` restriction
   produces a key that can read/write **only** under the user's prefix and is denied a
   sibling prefix; rotation creates-then-deletes; delete revokes.
10. **Saga crash/resume:** kill the service mid-create and mid-delete; the reconciler
    converges with no orphaned live B2 key or Secret.
11. **Rate limits** bite per-user and globally.

Where a real B2 key-API sandbox is unavailable in CI, gate those behind a tagged integration
job (like media-library's B2/MinIO split) — don't claim coverage you don't have.

---

## 14. Acceptance checklist

- [ ] Writes **only** `AnkiInstance` CRs + Secrets (PVC deletion is operator-owned via the CR
      data-fate field, §5.6 — zero workload-object verbs); a StatefulSet write path does not
      exist and is RBAC-denied.
- [ ] **Single-writer v1 (2026-07-11):** this service writes the whole `spec`; explicit stop
      uses `suspended` only, start sets `suspended: false` + `replicas: 1`, reset/reconnect bump
      `spec.restartedAt`, idle sleep sets `replicas: 0` — all reconciled by the operator, no
      live pod surgery.
- [ ] `internal.ensureConnected` (§5.7) enforces instance/tier/gate checks fail-closed with
      typed rejections, never auto-creates, and picks the correct lever; idle TTL (§5.8) and the
      `pro` gate + lapse flow (§5.9) behave per spec.
- [ ] `tunnel-credentials.json` (§6.4) always written per contracts.md §8c; the token is never
      persisted or logged (fingerprint only).
- [ ] `instance.create` runs the saga (B2 key → Secret → CR), is idempotent per user, and
      resumes after a crash; unrecoverable failure compensates (revokes the B2 key, deletes
      the Secret) — no orphaned live credential ever remains.
- [ ] `instance.delete` is two-phase (confirmation token + typed-name), honors the data-fate
      matrix (default destroy PVC + B2 prefix; `retainData` keeps them; **B2 key + Secret
      always killed**), and is a resumable saga.
- [ ] No AnkiWeb credential surface exists (2026-07 banner): no `ankiwebLogin` method, no
      sync-credentials Secret key, no serial, no password/hkey handling anywhere in the
      service.
- [ ] Per-user B2 key is `namePrefix`+`bucketId`-restricted, delivered where the pod/CSI
      expects, rotatable, and revoked on delete; the `applicationKey` is never stored.
- [ ] AuthN/Z reuses Keycloak; users manage only their own instance (edge-stamped
      keycloakId); admin ops ride the internal subject + ADMIN role and are audit-logged.
- [ ] Runs in-cluster with a minimal namespaced Role (CRs + Secrets only); holds the only
      k8s creds in the platform; the pod holds none.
- [ ] Status projects CR phase + provisioning state for the dashboard; emits per-user live
      events; exposes AGPL source-link metadata.
- [ ] Config fail-fasts at boot listing every missing var; secrets via Bitwarden→Vault→ESO;
      dev/prod split; OTEL + Pino + health + graceful shutdown wired per saas conventions.
- [ ] B2 applicationKey / tunnel token never logged (fingerprints only).
- [ ] Tests cover idempotency, authZ, delete-confirmation, redaction (unit) +
      envtest/kind k8s + B2-sandbox (integration).

---

## 15. Open questions (unresolved by the authoritative docs — flagged, not decided)

1. ~~**Service home (ARCHITECTURE §14, §2, §13 — explicitly OPEN).**~~ **RESOLVED (2026-07-11,
   user):** atomic service inside **`anki-mcp-saas`** → transport = **NATS behind the BFF**
   (§5.1 primary binding). The standalone-HTTP alternative is dead for v1.
2. **CRD contract for control-plane fields:**
   - ~~power-gate field~~ **RESOLVED ([contracts.md §2](./contracts.md)):** the gate is
     lifecycle-owned **`spec.suspended` (bool)**; the operator computes
     `effectiveReplicas = suspended ? 0 : replicas`. (The activator-honors-the-gate half is
     shelved with the activator, 2026-07-11 — the check now lives in §5.7.)
   - ~~the **reset** mechanism~~ **RESOLVED (2026-07-11 — ARCHITECTURE §16.6):**
     **`spec.restartedAt`** (RFC3339, lifecycle-written); operator copies it into the pod
     template as an annotation (rollout-restart pattern). §5.5.
   - ~~whether **PVC deletion** is a lifecycle-service verb or a CR field~~ **RESOLVED
     (2026-07-11 — ARCHITECTURE §16.7):** a **CR data-fate field the operator acts on**
     (e.g. `spec.dataRetention: Delete|Retain`; operator spec owns naming) — this service's
     RBAC stays free of workload-object verbs. §5.6, §9.
3. ~~**AnkiWeb `sync_login` wire mechanism (§6.1).**~~ **RESOLVED (2026-07-11, verified against
   Anki source at tag 25.09.2):** direct `hostKey` HTTP, implemented in Node — full shape in
   §6.1; `ANKIWEB_SYNC_LOGIN_MODE` dropped. A custom (self-hosted) sync server speaks the same
   sync-protocol `hostKey` action — only the `{endpoint}` prefix changes (allowlist still
   applies, §6.1) *(expected from the standard protocol; not separately verified against a
   self-hosted server)*.
4. ~~**B2 key model at scale (§6.2).**~~ **RESOLVED (2026-07-11, verified):** viable — **100M
   keys/account** documented cap; `bucketId`+`namePrefix` compose; per-user keys MUST add
   **`listAllBucketNames`**; the `writeKeys` key-manager credential is **master-equivalent**
   (backend-only); key create/delete must be **queued with backoff** (429/503, Class C);
   restriction inheritance for keys-created-by-restricted-keys is undocumented — don't rely on
   it. The media-library-model fallback is moot. (Prior-art caveat unchanged:
   `media-library-b2-provisioning.md` is prior art for bucket/region/Bitwarden wiring, not for
   per-user keys.)
5. ~~**B2 key delivery location (§6.2, ARCHITECTURE §10 layer 3, §12 item 9).**~~ **RESOLVED
   (2026-07-10 — the marker here was stale; [contracts.md §8b/§9](./contracts.md)):** the
   sidecar decision landed on an in-pod **Secret** — the separate `anki-<keycloakId>-b2` Secret,
   `envFrom`'d into the rclone sidecar only.
6. ~~**Secret-write vs sidecar refresh.**~~ **MOOT (2026-07):** the sync-credentials channel
   this item was about (Secret refresh / file-poll / restart-based pickup) was removed in v1
   entirely — see the top banner and §6.3. The Secret object now carries only the tunnel token
   (rotation = restart, §6.4).
7. ~~**Signup ordering with AnkiWeb creds.**~~ **MOOT (2026-07):** there are no AnkiWeb creds
   at signup — the user logs in inside Anki over VNC whenever they choose; instance creation
   never involves AnkiWeb auth (the saga's step 2 writes only the tunnel key). AnkiWeb login is
   never required for MCP (§5.7).
