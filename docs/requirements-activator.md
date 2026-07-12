# requirements-activator — Activator (data-plane proxy)

> **SHELVED FOR v1 (2026-07-11, user — see [ARCHITECTURE §16](./ARCHITECTURE.md) and
> [contracts.md](./contracts.md)):** v1 ships **without deploying the activator**. The 2026-07-11
> pivot made the SaaS **tunnel** the MCP door for hosted pods (the baked AnkiMCP add-on dials
> **out** to it — contracts.md §8c), and this component's remaining jobs were redistributed:
> **wake + request-hold** → tunnel (holds the MCP request) + lifecycle service (performs the
> wake, via a NATS ensure-connected call); **idle TTL + downscale trigger** → lifecycle service;
> **VNC door** → the new **VNC gateway** (dumb authenticated pipe: api-minted signed ticket,
> naming-convention pod resolution, no wake/k8s/DB). The CR spec is **single-writer (lifecycle)**
> for v1 — this doc's `spec.replicas` ownership is shelved with the component (contracts.md §2).
> The built Go code stays in `activator/`; formally retiring vs reviving it is **POSTPONED, not
> decided**. The body below is retained **unmodified** as the revival spec — read it as
> historical.

Implementation spec for the **activator**: the custom, stateless Go service that is the
**traffic front door** for Anki-as-a-Service. It wakes a suspended per-user pod on demand,
holds the request until the pod is Ready, forwards it, tracks idle activity, and triggers
graceful downscale. Written for an implementing agent.

**Authoritative parent:** [ARCHITECTURE.md](./ARCHITECTURE.md) — esp. §1 (product model),
§2 (component map), §5 (sync / graceful downscale), §7 (activator), §9 (dashboard/routing),
§10 (security). This doc does not re-decide anything closed there; where ARCHITECTURE is
silent or the grounding repo (`anki-mcp-saas`) doesn't answer a question, it goes to
**§13 Open questions** — no invented decisions.

Build order (ARCHITECTURE §14): addon → headless-anki image → operator → **activator** →
lifecycle service → dashboard. The activator depends on the **operator + `AnkiInstance`
CRD** existing (it patches CRs and watches the pods the operator creates) and on the
**headless-anki image** exposing the MCP + noVNC ports.

Target: **Go 1.23+**, `client-go` (typed clientset + `dynamic` client + informers),
`net/http` / `net/http/httputil`. Lives in this monorepo under `activator/`.

Shared cross-component values (GVR, CR field ownership, naming, namespace, labels, ports) are fixed
in [contracts.md](./contracts.md); this doc restates them for context and must match it. Key ones:
GVR `anki.ankimcp.ai/v1alpha1` (kind `AnkiInstance`); CR `metadata.name` = keycloakId; children
(StatefulSet/Service/Secret) = `anki-<keycloakId>`; namespace `anki-instances`; pod ports `mcp`
(3141) + `vnc-ws` (6080).

---

## 1. Goal & scope

### 1.1 Goal
Make a suspended (scaled-to-zero) per-user Anki pod behave, from the user's point of view,
like an always-on service: **a request during suspend is delayed a few seconds, never
rejected** (ARCHITECTURE §1). The activator is the only component that sees live user
traffic, so it also owns idle detection and the downscale trigger (§7, decision 7).

### 1.2 In scope (v1)
- **Transports:** HTTP, **MCP-over-HTTP** (Streamable HTTP + SSE), and **WebSocket**
  (browser noVNC). That is the complete v1 surface — browser noVNC arrives as a WebSocket,
  so **no raw-TCP path** is needed (ARCHITECTURE §7; raw TCP deferred to a possible
  native-VNC-client feature).
- **Wake-on-request:** resolve user → if the pod is scaled to zero, patch the user's
  `AnkiInstance` CR `spec.replicas: 0→1` → **hold** the connection until the pod is Ready →
  forward → record activity.
- **Idle TTL + downscale trigger:** per-user in-memory last-activity; on TTL expiry, patch
  `spec.replicas: 1→0`. The pod's own `preStop` runs the graceful quit + rclone drain
  (ARCHITECTURE §5) — the activator only flips the field.
- **Cold-start UX hooks:** a status endpoint the dashboard can poll while a pod warms.
- **Observability:** metrics + structured logs.

### 1.3 Out of scope (do NOT build here)
- **Raw TCP proxying** (deferred, §7).
- **Reconciling CRs into pods/PVCs/Services** — that is the operator's job (§8). The
  activator only ever patches **`spec.replicas`** and never any other CR field.
- **Creating/deleting instances, config toggles, reset** — the **lifecycle service** owns
  those (§2). The activator is read-mostly on the CR (patch replicas only).
- **The graceful-quit / rclone-drain sequence itself** — that lives in the pod's `preStop`
  (ARCHITECTURE §5). The activator triggers it by setting `replicas: 0`; it does not call
  `rclone rc` or quit Anki.
- **Persisting anything.** No database, no Redis (single-replica v1 — see §11).
- **Minting or refreshing auth tokens / sessions.** The activator is a *resource server*:
  it validates what the client presents (§4). Token issuance is Keycloak's; browser
  sessions are the `anki-mcp-saas` API's.

### 1.4 Statelessness (ARCHITECTURE §7 — load-bearing)
The activator holds **no durable state**:
- **"Who is awake"** is asked from Kubernetes (the API/informer cache is the source of truth),
  never cached authoritatively across restarts.
- **user → StatefulSet routing** is a **naming convention** (§5), not stored data.
- **last-activity timestamps** are **in-memory only**. Worst case after an activator
  restart: an idle user's clock resets and they get a fresh TTL hour before downscaling —
  explicitly acceptable (§7). Document this "restart amnesia = fresh TTL" behavior in the
  runbook; it is a feature, not a bug (no correctness impact — k8s still holds the real
  replica count, and a genuinely-idle pod costs one extra idle hour at most).

---

## 2. Request lifecycle (the core state machine)

Every inbound request/connection, regardless of transport, runs the same pipeline:

```
1. AUTHENTICATE   resolve the user identity from the request (§4).
                  ── unauthenticated → reject NOW, before touching k8s (DoS guard, §9). ──
2. RESOLVE        userIdentity → AnkiInstance CR name (naming convention, §5).
3. WAKE?          read desired/observed replicas from the informer cache.
                    replicas ≥ 1 and pod Ready  → skip to FORWARD.
                    replicas == 0 or not Ready  → enter HOLD.
4. HOLD           single-flight per user (§6): first caller patches spec.replicas 0→1;
                  all callers block on the same readiness signal (informer-driven, §7),
                  bounded by hold-timeout.
                    ready in time   → FORWARD.
                    timeout / error → typed error to client (§6.4).
5. FORWARD        reverse-proxy to the pod's Service/endpoint (§8).
6. RECORD         update in-memory last-activity for the user (§10). For WebSockets, the
                  connection counts as continuously active while open (§10.2).
```

A background **idle sweeper** (§10.3) runs independently of request handling and issues the
`replicas: 1→0` patch when a user's TTL lapses.

---

## 3. Routing scheme (ingress → activator → pod)

**One Cloudflare Tunnel → one activator Service → identity-based routing.** No per-user URLs,
no per-user subdomains, no second credential (ARCHITECTURE §9, decision 12). The Cloudflare
Tunnel routes **directly to the activator Service** (ARCHITECTURE §1/§10 — it bypasses
Traefik), so the activator terminates the public request and must do its own routing by
identity, not by URL path/host.

### 3.1 Proposed single-host, path-routed scheme
Serve everything on **one hostname** (e.g. `anki.<platform-domain>`), routed by path:

| Path | Transport | Auth carrier (§4) | User source |
|---|---|---|---|
| `POST/GET /mcp` | MCP-over-HTTP (Streamable HTTP + SSE) | `Authorization: Bearer <JWT>` | JWT `sub` (keycloakId) |
| `GET /vnc` (Upgrade: websocket) | WebSocket → noVNC/websockify in pod | `sid` httpOnly session cookie | server-side session → keycloakId |
| `GET /instance/status` | HTTP (JSON) | same as the caller's transport (cookie **or** Bearer) | resolved identity |
| `GET /healthz`, `/readyz` | HTTP | none (activator liveness/readiness — NOT a user pod) | — |
| `GET /metrics` | HTTP (Prometheus) | cluster-internal only (NetworkPolicy) | — |

Rationale for **path routing over host routing**: identity comes from the credential
(token/cookie), not the URL, so a single host suffices and there is nothing per-user to
encode in DNS. This keeps the Cloudflare Tunnel config to one ingress rule and matches the
existing `anki-mcp-saas` model, where the tunnel serves one static authenticated `/mcp`
surface and identity is carried by the token, not the URL (grounded in
`apps/tunnel/.../proxy-gate.service.ts`: "there are no per-tunnel OAuth surfaces anymore").

> **Note for the dashboard/lifecycle spec:** the noVNC page embeds `wss://anki.<domain>/vnc`
> and relies on the browser sending the `sid` cookie on the WS upgrade automatically (the
> `anki-mcp-saas` web app already authenticates its ORPC WebSocket exactly this way — the
> httpOnly `sid` cookie rides the upgrade, no tokenProvider; see
> `apps/web/src/components/AuthWsGate/AuthWsGate.tsx`). Cross-site cookie/`SameSite` behavior
> when the noVNC iframe is served from a different origin than `anki.<domain>` is an
> **open item (§13.3)**.

### 3.2 Pod-side targets
Each per-user pod (created by the operator) exposes:
- the **AnkiMCP HTTP port** (MCP server), and
- the **websockify/noVNC port** (WebSocket).

The activator forwards `/mcp` → the pod's `mcp` port (3141), `/vnc` → the pod's `vnc-ws` port
(6080). The per-user backend address is derived from the naming convention (§5) — the operator's
per-user **Service** DNS name
(`anki-<keycloakId>.anki-instances.svc.cluster.local:<port>`), resolved after readiness (§8).

---

## 4. Authentication (grounded in `anki-mcp-saas`)

**AuthN happens before any k8s interaction (§9).** There are **two schemes**, one per client
class, both resolving to the same identity key: the **keycloakId** (Keycloak `sub` /
`users.keycloak_id`).

### 4.1 MCP-over-HTTP → OAuth 2.1 Bearer JWT (stateless)
This mirrors, byte-for-byte in behavior, the existing MCP auth in `anki-mcp-saas`
(`packages/mcp-auth/src/mcp-token-verifier.ts`, `mcp-auth.guard.ts`; tunnel wiring in
`apps/tunnel/src/keycloak/`). Reimplement the same contract in Go:

1. Extract the token from the **`Authorization: Bearer` header only** (never a query param).
2. **Verify statelessly against the Keycloak realm JWKS** — signature, `iss`, `exp`, **and
   `aud`**. `aud` validation is REQUIRED ("MCP resource servers MUST only accept tokens
   intended for themselves"). Go equivalent of the repo's `jose` core: `github.com/lestrrat-go/jwx/v2`
   (`jwk.Cache` for an auto-refreshing remote JWKS + `jwt.Parse` with issuer/audience
   validation), or `MicahParks/keyfunc` + `golang-jwt/jwt`.
3. **`sub` is the identity.** Its presence is required; `sub` == keycloakId (used directly in
   §5 routing). The `anki-mcp-saas` tunnel additionally does a fail-closed **Users-service
   account check** (`getUserByKeycloakId` → 401 if the row is gone) as a deletion guard
   (`user-principal-resolver.ts`). The activator SHOULD do the equivalent so a deleted user
   cannot wake a pod — **but where it checks is open (§13.2)**: it has no DB access by
   design, and a deleted user should have had their `AnkiInstance` CR deleted by the
   lifecycle service anyway, in which case §5 resolution already fails closed (no CR → 404,
   no wake). Recommended v1: rely on **"no CR ⇒ no wake"** as the account gate and skip a
   Users-service dependency; revisit if CR deletion can lag account deletion.
4. On any failure emit **RFC 6750 / RFC 9728** challenges exactly as the repo does: missing
   token → `401` with `WWW-Authenticate: Bearer resource_metadata="<PRM URL>"` (no error
   code, RFC 6750 §3.1); invalid/expired/wrong-aud → add `error="invalid_token"`. Serve the
   **protected-resource metadata** (RFC 9728) for the activator's own resource URL.

**The activator is its own MCP resource server** and therefore needs its **own Keycloak
client scope + Audience mapper** stamping `aud = https://anki.<domain>/mcp` — the same
one-scope-per-service pattern the repo uses (`tunnel-mcp`, `studio-*`; see the repo's
`docs/mcp-auth-unification/keycloak-realm-setup.md`). **This realm change is a prerequisite
infra task** — call it out to the infrastructure repo. `aud`, `issuer`, and the JWKS URI are
config, not hardcoded (§7); reuse the repo's internal/public Keycloak URL split (JWKS fetched
via the internal URL, `iss` matched against the public URL — Keycloak stamps `iss` from the
browser-facing authorize URL).

### 4.2 Browser noVNC → `sid` session cookie (BFF, opaque)
The `anki-mcp-saas` web app uses a **cookie-BFF** model (`apps/web/src/lib/auth.ts`): the
browser holds **no tokens**; an **httpOnly `sid` cookie** rides every request and every WS
upgrade automatically; the server holds all OAuth tokens; session state is validated
server-side (`/api/auth/me`). The `sid` cookie is **opaque** to the activator.

**This is the hard integration point.** The activator is a separate Go service and cannot
decode `sid` itself. It must resolve `sid → keycloakId` through the API. **The exact
mechanism is open (§13.1)** — candidate approaches, in rough preference order:
- **(a) Internal session-introspection endpoint.** The `anki-mcp-saas` API exposes a small,
  cluster-internal, authenticated endpoint (e.g. `POST /internal/session/resolve`) that takes
  the `sid` cookie and returns `{ keycloakId }` (or 401). The activator calls it on WS upgrade
  and caches the positive result for the connection's lifetime. Simple; adds a control-plane
  dependency on the VNC hot path (mitigated: one call per WS connect, not per frame).
- **(b) Shared verifiable session token.** Have the API mint a short-lived **signed** VNC
  ticket (JWT with `aud = anki-vnc`, `sub = keycloakId`) that the dashboard passes to the WS
  (subprotocol or a first in-band message), so the activator verifies it statelessly with the
  same JWKS/HS-key machinery as §4.1 — no per-connect round-trip. Cleaner but needs a new
  dashboard flow.
- **(c) Shared session store.** Activator reads the same session backing store as the API.
  Rejected for v1: couples the stateless activator to the API's session storage and secrets.

Whichever is chosen, **authN must complete before any wake (§9)** and the WS upgrade must be
rejected (`401`/`403`, connection closed) if the session is invalid.

### 4.3 MCP auth passthrough to the pod (OPEN — §13.4)
Whether the activator **forwards** the validated Bearer token to the pod's AnkiMCP server, or
**terminates** auth at the activator and forwards over the trusted cluster-internal network
(the pod's NetworkPolicy allows ingress *only* from the activator — ARCHITECTURE §10 Layer 1),
depends on whether the baked AnkiMCP add-on expects its own auth in the hosted image. The
standalone AnkiMCP server has its own auth surface; in-cluster it may run authless behind the
activator. **Flagged open (§13.4).** Default assumption for v1: **terminate at the activator,
forward to an authless pod-internal MCP port** (defence-in-depth is the NetworkPolicy), and
strip the client `Authorization` header on forward unless §13.4 resolves to passthrough.

---

## 5. User → instance routing (naming convention)

Routing is a **pure function of identity**, computed, not stored (§7). The keys below are the
**decided shared contract** ([contracts.md §§3, 5, 7](./contracts.md)):

```
crName      = keycloakId                              # AnkiInstance CR metadata.name (the raw UUID)
childName   = "anki-" + keycloakId                    # StatefulSet / Service / Secret
serviceHost = childName + "." + ANKI_NAMESPACE + ".svc.cluster.local"
podSelector = "app.kubernetes.io/name=anki-instance, anki.ankimcp.ai/user=" + keycloakId
```

- A Keycloak `sub` is a UUID (`[0-9a-f-]`) → **already RFC-1123-safe**, so it is used verbatim as
  the CR name; children carry the `anki-` prefix. No `slug()` transform is needed for well-formed
  keycloakIds (validate/reject anything not RFC-1123).
- **keycloakId is the identity/routing key everywhere** — stable, unique, never renamed, and
  exactly what both auth schemes yield. `spec.user` carries the same keycloakId (not the AnkiWeb
  username, which is display-only). This matches `requirements-operator.md` and
  `requirements-lifecycle-service.md`.
- **No CR for the identity ⇒ fail closed:** return `404` (MCP: JSON-RPC not-found, matching
  the repo's `-32001`) / close the WS. Never create a CR (that is the lifecycle service's
  job) and never wake anything for an identity with no instance. This doubles as the
  account-deletion gate (§4.1 step 3).

---

## 6. Hold semantics (wake + single-flight + timeout)

### 6.1 Single-flight per user (thundering-herd guard)
N concurrent requests arriving during one cold start MUST cause **exactly one** wake (one CR
patch) and **all N** must be held on the **same** readiness signal — no patch storms, no N
parallel 0→1 races. Implement a **wake coordinator**: a `map[identity]*wakeState` guarded by a
mutex; the first caller for an identity creates the `wakeState`, issues the patch, and
registers interest in the readiness signal; subsequent callers find the existing `wakeState`
and subscribe to it. Use a broadcast primitive (a `chan struct{}` closed on ready, or
`sync.Cond`) so one informer event releases all waiters.

> `golang.org/x/sync/singleflight` is a tempting fit but its result is delivered to the
> in-flight callers of one `Do` — it does not natively model "wake now, many independent HTTP
> handlers block until an external informer event fires." A small bespoke coordinator keyed by
> identity is clearer and testable with a fake clock + fake informer. Either is acceptable if
> the herd guarantee (one wake, all held) holds.

### 6.2 The wake action — patch `spec.replicas` only
Read desired replicas from the informer cache; if `0` (or the pod is absent/not Ready), issue
a **merge patch limited to the single field**:

```go
// dynamic client on the AnkiInstance GVR; MergePatch touches ONLY spec.replicas.
patch := []byte(`{"spec":{"replicas":1}}`)
dyn.Resource(ankiInstanceGVR).Namespace(ns).
    Patch(ctx, crName, types.MergePatchType, patch, metav1.PatchOptions{})
```

- Use `client-go`'s **`dynamic.Interface`** for the CRD (unstructured) — the activator does
  not need generated CR types (client-go ARCHITECTURE: dynamic client for CRDs).
- Patch must be **idempotent and field-scoped**: a merge patch of `{"spec":{"replicas":1}}`
  never disturbs other spec fields. Consider **Server-Side Apply** with a distinct field
  manager if concurrent writers on the CR are a concern, but v1's single activator replica +
  single-flight makes a plain merge patch sufficient. (ARCHITECTURE §12.11 notes SSA is
  advisable cluster-wide for the operator/CRD apps due to racing ArgoCD sync — the *operator*
  should use SSA; the activator's one-field replica bump does not need it.)
- If the patch fails (conflict/transient) → bounded retry with backoff; if it keeps failing →
  release waiters with a typed error (§6.4). Never spin.
- **Honor the power gate.** If the CR's lifecycle-owned **`spec.suspended == true`**
  ([contracts.md §2](./contracts.md)), the operator forces `effectiveReplicas = 0`, so patching
  `spec.replicas: 1` would never bring the pod up (the hold would just time out). When `suspended`,
  the activator **does not wake**: it returns a typed "instance stopped" response (MCP: a retryable
  error naming the stopped state; VNC: close before upgrade) instead of holding. The activator
  never writes `suspended` — only the lifecycle service does.

### 6.3 Readiness — informer-driven, NOT polling (ARCHITECTURE §7)
Hold until the pod is actually serving. Use a **`SharedInformerFactory`** (typed clientset)
**scoped to the Anki namespace and label-filtered** via
`informers.WithTweakListOptions(func(o *metav1.ListOptions){ o.LabelSelector = "app.kubernetes.io/name=anki-instance" })`,
watching **Pods** (readiness = the `Ready` condition true) and/or **EndpointSlices** (an
address present in the per-user Service's slice — a stronger "actually routable" signal). On
each add/update event, look up any waiting `wakeState` for that identity and release it.

- **Readiness gate (recommend):** pod `Ready` condition true **AND** the per-user Service has
  a ready endpoint. Endpoint-present is the truest "the proxy can connect now" signal and
  avoids a forward that races kube-proxy/Cilium programming.
- Informers give a **shared, cached, watch-based** view — no per-request GET/LIST loops
  against the API server (ARCHITECTURE §7 "informer-driven, not polling"). The informer's
  `WatchFunc`/`ListFunc` pattern is the client-go idiom (verified against current client-go
  `informers/core/v1/pod.go` + `SharedInformerFactory`).
- A relist/resync interval (e.g. 5–10 min) is a backstop only; correctness comes from watch
  events.

### 6.4 Hold timeout + what the client gets
Config `HOLD_TIMEOUT` (default **60s** — cold start is pod schedule + image pull-if-cold +
Anki boot + rclone mount; size generously, see §13.6). On expiry:
- **MCP-over-HTTP:** respond `503 Service Unavailable` with `Retry-After` and a **JSON-RPC
  error** body (reuse the repo's error-code convention where sensible) whose message tells the
  LLM client to retry (a structured "retry after N s" is obeyed automatically — same principle
  as ARCHITECTURE §6's LLM-retryable hkey UX). Do **not** hang forever; a bounded, retryable
  error beats an indefinitely stalled request.
- **WebSocket (noVNC):** if the upgrade cannot complete in time, either (a) fail the upgrade
  with `503` before switching protocols, or (b) if already upgraded for UX reasons, send a
  close frame with a policy code and a reason string the dashboard maps to a "still
  starting — retry" toast. Prefer (a): hold the wake **before** completing the upgrade so the
  browser sees a clean retryable failure.
- Emit a `cold_start_timeout` metric + a structured warn log (identity, waited duration).

### 6.5 WebSocket-upgrade holding
For `/vnc`, run the full wake+hold pipeline **before** completing the `101 Switching
Protocols` handshake, then dial the pod and pump bytes bidirectionally
(`net/http/httputil.ReverseProxy` handles the upgrade in Go 1.20+, or a hijack + `io.Copy`
pair). The user perceives a slightly slow connect on a cold pod, never an error
(ARCHITECTURE §1).

### 6.6 Streaming / SSE for MCP
MCP-over-HTTP is **Streamable HTTP**: responses may be `text/event-stream` (SSE) or chunked.
The reverse proxy MUST **not buffer** these — disable response buffering, flush per chunk
(`http.Flusher` / `ReverseProxy.FlushInterval = -1`), and propagate the request context so a
client disconnect cancels the upstream. Long-lived SSE streams also **count as active traffic**
for TTL (§10) for their duration.

### 6.7 Cold-start UX hook — `GET /instance/status`
An authenticated status endpoint the dashboard polls to show a "starting…" state
(ARCHITECTURE §1's "user sees a slow response, never an error" — surfaced as UI):

```json
GET /instance/status  → 200
{ "state": "suspended" | "starting" | "ready", "replicas": 0|1, "since": "<RFC3339>" }
```

`state` is derived from the informer cache (desired replicas + pod Ready), never a fresh API
call. Same auth as the calling transport (§4). This endpoint **must not itself trigger a
wake** — it is read-only observation (otherwise a polling dashboard would defeat idle
downscale). The dashboard decides whether to wake (by opening the VNC WS / issuing an MCP
call), not by polling status.

---

## 7. Configuration surface

All knobs are env vars (12-factor; no hardcoded hosts/timeouts — mirrors the repo's config
discipline). Proposed defaults:

| Env var | Default | Meaning |
|---|---|---|
| `LISTEN_ADDR` | `:8080` | public HTTP/WS listen address (behind the Cloudflare Tunnel) |
| `METRICS_ADDR` | `:9090` | Prometheus `/metrics` (cluster-internal only) |
| `ANKI_NAMESPACE` | `anki-instances` | namespace holding `AnkiInstance` CRs + per-user pods/Services |
| `ANKI_INSTANCE_GROUP` / `_VERSION` / `_RESOURCE` | `anki.ankimcp.ai` / `v1alpha1` / `ankiinstances` | CRD GVR (contracts.md §1; must match operator) |
| `INSTANCE_LABEL_KEY` | `anki.ankimcp.ai/user` | pod/Service label carrying the keycloakId (contracts.md §5) |
| `HOLD_TIMEOUT` | `60s` | max time to hold a request during cold start (§6.4) |
| `IDLE_TTL` | `1h` | per-user inactivity before downscale (ARCHITECTURE §1/§7) |
| `IDLE_SWEEP_INTERVAL` | `30s` | how often the idle sweeper runs (§10.3) |
| `WAKE_RATE_LIMIT` | e.g. `5/min per identity` | cap on wake patches per user (§9) |
| `MCP_ISSUER` / `MCP_JWKS_URI` / `MCP_AUDIENCE` | — | Keycloak realm issuer (public URL), JWKS (internal URL), resource `aud` (§4.1) |
| `MCP_RESOURCE_URL` | `https://anki.<domain>/mcp` | this resource server's canonical URL (PRM, aud) |
| `SESSION_RESOLVE_URL` | — | internal endpoint to resolve `sid → keycloakId` (§4.2 option a; open §13.1) |
| `POD_MCP_PORT` / `POD_VNC_PORT` | `3141` / `6080` | pod container ports for MCP (`mcp`) / websockify (`vnc-ws`) — contracts.md §7 |
| `FORWARD_BEARER_TO_POD` | `false` | passthrough vs terminate (§4.3, open §13.4) |
| `PROXY_READ/WRITE/IDLE_TIMEOUT` | tuned per transport | proxy timeouts; **WS/SSE need effectively no idle timeout** or a long one |

Header handling: strip hop-by-hop headers, set `X-Forwarded-*`, preserve/inject a correlation
id header for tracing. For SSE/WS, ensure read/write/idle timeouts do **not** kill long-lived
streams (a common footgun — set per-route).

---

## 8. Forwarding (reverse proxy)

- **HTTP + MCP:** `net/http/httputil.NewSingleHostReverseProxy` (or a hand-rolled
  `ReverseProxy` with a custom `Director`/`Rewrite`) targeting the per-user Service host:port
  (§5). Set `FlushInterval = -1` for streaming (§6.6). Propagate request context for cancel.
- **WebSocket:** Go's `ReverseProxy` transparently proxies `Upgrade` requests since Go 1.20;
  otherwise hijack the conn and `io.Copy` both directions. Honor the same per-user target.
- **Target resolution:** forward to the operator's **per-user Service** DNS name (stable
  across pod restarts), not a pod IP. Only forward **after** the readiness gate (§6.3) so the
  Service has a ready endpoint — this avoids connection-refused races during 0→1.
- **Connection failures after readiness** (pod died mid-request): return a retryable
  `502/503` (MCP) or close the WS with a retryable reason; do **not** re-trigger a wake
  storm — one bounded re-check of readiness, then fail.
- Reuse upstream connections (keep-alive) per target where the transport allows.

---

## 9. Security

Grounded in ARCHITECTURE §10 (own the whole isolation story — greenfield) and the repo's
auth model (§4).

1. **AuthN before any wake — the primary DoS guard.** An **unauthenticated request must not
   scale anything.** Steps 1→2 of §2 run entirely in-process (token/cookie validation +
   naming-convention lookup) before any CR patch. An anonymous flood costs only JWKS-cached
   signature checks, never a pod spin-up. This is non-negotiable — without it, `/mcp` and
   `/vnc` are a trivial "scale the whole fleet" amplification vector.
2. **Rate-limit wakes per identity.** Even authenticated, cap wake patches per user
   (`WAKE_RATE_LIMIT`) so a compromised/abusive account can't thrash 0↔1. Single-flight (§6.1)
   already collapses concurrent requests into one wake; the rate limit bounds *sequential*
   churn. (The repo has prior art for per-identity friction/throttling —
   `apps/tunnel/src/friction/`, `logged-throttler.guard.ts` — mirror the structured-log-on-
   throttle style.)
3. **Least-privilege RBAC (§12).** The activator's ServiceAccount may:
   - `get`, `list`, `watch`, **`patch`** on `ankiinstances` (the CRD) — patch is needed for
     `spec.replicas` both directions;
   - `get`, `list`, `watch` on `pods` and `endpoints`/`endpointslices` (readiness informers).
   Nothing else. **No** create/delete on any resource, no Secrets, no access to other
   namespaces, no `exec`. Scope to `ANKI_NAMESPACE` with a `Role`/`RoleBinding` (namespaced)
   rather than cluster-wide where the CRD is namespaced.
4. **Runs under PSA `baseline`** (ARCHITECTURE §10 Layer 3): `runAsNonRoot`, drop all
   capabilities, no privilege escalation, read-only root FS, resource limits. The activator is
   a plain Go binary with no FUSE/privileged needs, so `baseline` (even `restricted`) is
   trivially satisfiable — unlike the Anki pod's FUSE tension.
5. **NetworkPolicy:** ingress on `LISTEN_ADDR` from the Cloudflare Tunnel only; `/metrics`
   reachable only from the o11y stack; egress to the Anki pods' Services, the k8s API, and
   Keycloak (JWKS). The activator is a stateless component and (per ARCHITECTURE §3) can run
   on dedicated/stateless nodes, not pinned to Cloud-Volume nodes.
6. **Never log secrets.** No tokens, no `sid`, no hkey. Log a token/subject **fingerprint**
   (e.g. first 8 of `sha256(sub)`) at most — same discipline as ci-buddy §B.8 and the repo's
   `packages/logger/src/redact.ts`.

---

## 10. Idle tracking & TTL downscale

### 10.1 What counts as activity
**Any traffic on any transport** updates the user's in-memory `lastActivity` timestamp: an
MCP request/response, an HTTP request, a VNC WS frame, an SSE chunk. This is the only signal
(no pod-side introspection) — the activator is the sole live-traffic observer (ARCHITECTURE §7).

### 10.2 Open WebSocket = continuously active (precise definition)
An **open VNC WebSocket keeps the user active for as long as it is open**, independent of
whether bytes are flowing (a user staring at a card sends no frames but is not idle).
Implement with a per-user **open-connection counter**, not just a timestamp:

```
onWSOpen(id):   openConns[id]++ ; touch(id)
onWSClose(id):  openConns[id]-- ; touch(id)      # start the TTL clock from close
touch(id):      lastActivity[id] = clock.Now()
isIdle(id):     openConns[id] == 0 && clock.Now()-lastActivity[id] > IDLE_TTL
```

So the rule is **existence-based while open, traffic-based after close**: a live WS pins the
user active (never downscaled mid-VNC-session); once the last WS closes, the normal
timestamp-TTL clock runs. Long-lived MCP SSE streams are treated the same way (an open stream
increments the counter). Also **refresh `lastActivity` on WS frames** so that if the counter
logic is ever bypassed, traffic still counts — belt and suspenders.

### 10.3 The idle sweeper (downscale trigger)
A background goroutine ticks every `IDLE_SWEEP_INTERVAL`. For each tracked identity with
`isIdle(id)` true: **patch `spec.replicas: 1→0`** (same dynamic-client field-scoped merge
patch as §6.2, value `0`) and drop the identity from the in-memory maps.

- The activator **only flips the field**. The pod's **`preStop`** hook runs the graceful
  sequence — quit Anki cleanly (close-time auto-sync) → drain rclone via `rclone rc` → then
  the container exits; the StatefulSet scales to 0 (ARCHITECTURE §5, decision 8). The
  activator does **not** implement quit/drain and does **not** wait for it (fire-and-forget
  the patch; k8s + `terminationGracePeriodSeconds` own the drain window).
- Use a **fake clock** (`k8s.io/utils/clock/testing.FakeClock`) behind a `clock.Clock`
  interface so the sweeper + TTL are unit-testable without real time.

### 10.4 Restart amnesia (documented, acceptable)
On activator restart the in-memory maps are empty. A user whose pod is still `replicas: 1`
but who is genuinely idle will not be downscaled until they next generate traffic (which
resets their clock) or until… they never do — meaning a **stranded warm pod** could persist.
Mitigation: on startup, **seed the tracker from k8s** — list `AnkiInstance` CRs with
`replicas: 1` and initialize their `lastActivity = startupTime` (fresh TTL). This bounds the
waste to one TTL per warm pod after a restart (ARCHITECTURE §7's stated acceptable worst case)
**and** guarantees eventual downscale of pods that were warm before the restart. This seeding
is the one k8s read that reconciles statelessness with "no pod stays warm forever."

---

## 11. Multi-replica note (v1 = single replica)

v1 runs **one activator replica** (ARCHITECTURE §7): in-memory TTL + single-flight are
process-local, correct only when one process sees all of a user's traffic. Run
`replicas: 1`, `Recreate`/`RollingUpdate maxUnavailable: 1` — a brief blip during rollout is
acceptable (a held request retries; §6.4). If HA/scale later demands multiple replicas, the
options (explicitly deferred, §7): **consistent per-user routing** (hash identity → replica)
or a **small shared timer/lock store (Redis)** for TTL + single-flight. Do **not** build this
in v1; just don't design in a way that forbids it (keep TTL/single-flight behind interfaces).

---

## 12. Implementation sketch (Go, under `activator/`)

```
activator/
├── cmd/activator/main.go        # wiring: config, k8s clients, informers, HTTP server, sweeper
├── internal/config/             # env parsing, validation (fail fast on missing MCP_* etc.)
├── internal/auth/
│   ├── mcp.go                   # Bearer JWT verify (jwx): iss/exp/aud, PRM + WWW-Authenticate
│   └── session.go               # sid → keycloakId (open §13.1); interface + chosen impl
├── internal/routing/route.go    # identity → crName/labels/serviceHost (pure fn; unit-tested)
├── internal/wake/
│   ├── coordinator.go           # single-flight per identity, hold, timeout (fake-clock testable)
│   └── patch.go                 # dynamic-client MergePatch of spec.replicas (0/1)
├── internal/readiness/informer.go # SharedInformerFactory: pods + endpointslices; release waiters
├── internal/idle/tracker.go     # lastActivity + openConns maps, clock.Clock, isIdle
├── internal/idle/sweeper.go     # ticker → downscale patch; startup seed from CRs (§10.4)
├── internal/proxy/
│   ├── http.go                  # httputil.ReverseProxy, FlushInterval=-1 for SSE
│   └── ws.go                    # websocket upgrade proxying (hold before 101)
├── internal/server/handlers.go  # /mcp, /vnc, /instance/status, /healthz, /readyz
├── internal/metrics/            # Prometheus collectors (§12.2)
└── internal/observ/log.go       # structured logging (slog), fingerprint redaction
```

**k8s clients:** typed `kubernetes.Clientset` (pods/endpoints informers) **+**
`dynamic.DynamicClient` (AnkiInstance CR patch, on the configured GVR). In-cluster config via
`rest.InClusterConfig()`.

**Pure-logic seams (no k8s/net import → directly unit-testable):** the routing function
(§5), TTL/`isIdle` logic (§10), single-flight coordinator state transitions (§6.1), token
claim validation shaping (§4.1). Mirror ci-buddy REQUIREMENTS §6's "plain functions, no
framework import" discipline.

### 12.2 Observability
- **Metrics (Prometheus):**
  - `activator_cold_starts_total` (counter, by result: ready/timeout/error)
  - `activator_hold_duration_seconds` (histogram — cold-start hold time)
  - `activator_active_users` (gauge — identities with `replicas:1` / open conns)
  - `activator_open_connections` (gauge, by transport)
  - `activator_wakes_total` / `activator_downscales_total` (counters)
  - `activator_wake_patch_errors_total`, `activator_auth_failures_total` (by scheme/reason)
  - `activator_proxy_requests_total` + latency histogram (by transport, status)
- **Structured logs (slog/JSON):** one line per wake (identity fingerprint, waited duration,
  result), per downscale (identity, idle duration), per auth failure (scheme, reason — never
  the token), per throttle. Include a correlation id propagated to the pod. No secrets (§9.6).
- **Tracing (optional v1):** the repo uses OpenTelemetry (`instrumentation.ts`,
  `trace-context.util.ts`); propagate `traceparent` through the proxy for continuity even if
  the activator itself doesn't export spans in v1.

---

## 13. Testing

### 13.1 Unit (fast, no cluster)
- **Routing table (§5):** identity → crName/labels/host; RFC-1123 slug edge cases; unknown
  identity → not-found.
- **TTL logic (§10)** with a **fake clock**: activity resets the clock; open WS pins active
  regardless of elapsed time; last-close starts the TTL; `isIdle` boundary at exactly `TTL`.
- **Single-flight (§6.1):** N concurrent waiters → exactly **one** wake patch, all released on
  one readiness event; timeout releases all with the typed error; patch error path.
- **Auth (§4.1):** valid/expired/wrong-`aud`/missing-`sub`/malformed-header → correct
  `WWW-Authenticate` challenge shape (assert RFC 6750/9728 header bytes, as the repo's
  `challenge.spec.ts` does); use a local JWKS + minted test tokens (jwx test keys).
- **k8s interactions** with **`k8s.io/client-go/kubernetes/fake`** +
  **`k8s.io/client-go/dynamic/fake`**: assert the patch is a merge patch touching **only**
  `spec.replicas`, with the right value (0/1), on the right GVR/name.

### 13.2 Integration (envtest / kind)
Use **`sigs.k8s.io/controller-runtime/pkg/envtest`** (real API server + etcd, no kubelet) for
CR-patch + informer wiring, and **kind** where a real scheduler/kubelet is needed for actual
pod readiness:
- **Wake-on-request:** request to a `replicas:0` instance → CR patched to `1` → (kind) pod
  becomes Ready → request forwarded → 200/expected body.
- **Hold-until-ready:** request blocks, informer fires on Ready, request completes; assert
  hold duration ≈ readiness time, not a fixed poll interval.
- **Herd:** 50 concurrent requests during one cold start → one patch, all served.
- **Idle-downscale:** after `IDLE_TTL` (fake clock or shrunk TTL) with no traffic → CR patched
  to `0`; an open WS prevents downscale until it closes.
- **Auth gate:** unauthenticated `/mcp` and `/vnc` → rejected with **zero** CR patches
  (assert the fake/real client saw no write) — the DoS guard (§9.1) as an executable test.

### 13.3 Load / soak
Held-connection **memory**: open thousands of concurrent held WS/SSE connections during
simulated cold starts and measure per-connection footprint (goroutines + buffers) — the
activator holds many connections open by design, so this is the scaling axis to watch
(ties to ARCHITECTURE §12.10 capacity planning on 3× CX43). Note the goroutine-per-connection
cost and set sensible buffer sizes; confirm held connections release cleanly on timeout
(no leak).

---

## 14. Acceptance checklist
- [ ] `/mcp` validates OAuth Bearer JWT against the Keycloak realm JWKS (sig/iss/exp/**aud**),
      emits RFC 6750/9728 challenges on failure, and serves its own PRM.
- [ ] `/vnc` authenticates the browser session (`sid`) and rejects invalid sessions **before**
      completing the WS upgrade.
- [ ] **No unauthenticated request ever causes a CR patch / wake** (test-asserted, §13.2).
- [ ] Identity → `AnkiInstance` CR resolved by naming convention; **no CR ⇒ 404/close, no wake**.
- [ ] Cold request patches **only** `spec.replicas: 0→1` (merge patch, dynamic client), holds
      **single-flight** per user, releases on an **informer** readiness event (not polling),
      forwards on ready.
- [ ] Hold respects `HOLD_TIMEOUT`; on timeout the client gets a **retryable** typed error
      (MCP 503+Retry-After / clean WS failure), never an indefinite hang.
- [ ] MCP SSE / Streamable-HTTP responses stream unbuffered; WS upgrades proxy bidirectionally.
- [ ] `GET /instance/status` reports suspended/starting/ready from the informer cache and does
      **not** trigger a wake.
- [ ] Per-user idle TTL tracked in-memory; **open WS pins the user active**; last-close starts
      the TTL clock; sweeper patches `spec.replicas: 1→0` on expiry.
- [ ] Downscale only flips the field — pod `preStop` owns quit + rclone drain (activator does
      not implement/await it).
- [ ] Restart seeds the tracker from `replicas:1` CRs so no warm pod is stranded (§10.4).
- [ ] RBAC limited to `patch/get/list/watch ankiinstances` + `get/list/watch pods/endpoints`;
      nothing else. Runs under PSA `baseline` (nonroot, caps dropped, RO rootfs, limits).
- [ ] Wake rate-limited per identity; single-flight collapses concurrent wakes.
- [ ] Metrics (cold starts, hold time, active users, wakes/downscales) + structured logs;
      **no token/`sid`/hkey ever logged** (fingerprint only).
- [ ] Unit tests (routing, TTL, single-flight, auth) with fake clock + fake k8s client;
      integration tests (wake, hold-until-ready, idle-downscale, herd, auth-gate) on
      envtest/kind; a load-test note for held-connection memory.

---

## 15. Open questions

1. **`sid` session resolution for `/vnc` (§4.2) — the biggest one.** Genuinely open; collected in
   [contracts.md → Cross-cutting open decisions #1](./contracts.md). The browser session is an
   **opaque httpOnly `sid` cookie** validated server-side by the `anki-mcp-saas` API (cookie-BFF;
   `apps/web/src/lib/auth.ts`); the activator cannot decode it. Options: (a) internal
   `sid → keycloakId` introspection endpoint, (b) a short-lived signed VNC ticket (stateless
   verify, preferred for decoupling), (c) shared session store (rejected for v1). Needs a decision
   with the `anki-mcp-saas` owner; dictates a small dashboard/API change.
2. ~~**Account-deletion gate placement (§4.1 step 3).**~~ **RESOLVED (2026-07-10):** rely on
   **"no CR ⇒ no wake"** — the lifecycle service deletes the `AnkiInstance` CR on account deletion
   (requirements-lifecycle-service §5.6 delete saga: `CR → pod gone → PVC → B2 → Secret`), so §5
   resolution already fails closed (404, no wake) with no Users-service dependency. Residual: the
   delete saga must remove the CR promptly; a just-deleted user can otherwise wake a lingering CR
   until the saga's `CR` step runs (it is ordered first, and retried until it succeeds).
3. **Cross-origin cookie / `SameSite` for the embedded noVNC iframe (§3.1).** If the dashboard
   serves noVNC from a different origin than `anki.<domain>`, the `sid` cookie may not ride the
   WS upgrade (`SameSite`/third-party-cookie rules). May force option (b) of §13.1 or a
   same-origin embedding. Needs the dashboard's final embedding decision.
4. **MCP auth passthrough vs terminate (§4.3).** Genuinely open; collected in
   [contracts.md → Cross-cutting open decisions #2](./contracts.md). Default assumed: terminate at
   activator, strip `Authorization`, forward to an authless pod-internal MCP port; resolve with the
   headless-anki image spec.
5. ~~**Naming-convention contract with the operator (§5).**~~ **RESOLVED
   ([contracts.md §§3, 5](./contracts.md)):** CR `metadata.name` = keycloakId; children
   `anki-<keycloakId>` (StatefulSet/Service/Secret); label `anki.ankimcp.ai/user=<keycloakId>`;
   Service DNS `anki-<keycloakId>.anki-instances.svc.cluster.local`. `spec.user` = keycloakId
   (username is display-only).
6. **Cold-start budget for `HOLD_TIMEOUT` (§6.4).** Real 0→1 latency = schedule + (cold) image
   pull + Anki boot + rclone mount + Hetzner Cloud Volume attach. ARCHITECTURE §12.4 warns of
   a **~6-minute multi-attach force-detach tail** when a node dies ungracefully — that is a
   worst case that will blow any reasonable hold timeout. Decide the normal-case default (60s
   assumed) and how the UX degrades on the pathological attach-tail case (the status endpoint
   + a "still starting, retry" message, rather than a longer hang).
7. **Keycloak realm change is a prerequisite (§4.1).** The activator needs its own MCP client
   scope + Audience mapper (`aud = https://anki.<domain>/mcp`). This must be added to the realm
   (infra repo) before the `/mcp` surface can authenticate anything — same pattern as
   `tunnel-mcp`/`studio-*`. Flag to the infrastructure repo.
8. ~~**`AnkiInstance` GVR + `replicas` field shape (§6.2).**~~ **RESOLVED
   ([contracts.md §§1, 2](./contracts.md)):** GVR is **`anki.ankimcp.ai/v1alpha1`** (not the
   earlier `ankimcp.io` guess); the activator owns and patches **`spec.replicas` (0↔1)** on the CR
   as its exclusive field; the lifecycle-owned `spec.suspended` gate sits alongside it and the
   operator computes `effectiveReplicas = suspended ? 0 : replicas`.
