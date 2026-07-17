# anki-mcp-saas — Dashboard GUI (v1) Requirements

> **HANDOFF (2026-07-10):** implemented + **designed** by the **`anki-mcp-saas` agent** (Vite 8 +
> React 19 SPA, oRPC-over-WS — confirmed against the real `apps/web`). This doc is the
> self-contained spec; before implementing, read **`contracts.md`** and **`ARCHITECTURE.md`**
> §1/§9, and consume the lifecycle service's API (`requirements-lifecycle-service.md`) ~~+ the
> activator's `/instance/status` endpoint (`requirements-activator.md`)~~ **(2026-07-11: the
> activator is shelved — VNC rides the VNC gateway, §6.2; wake progress = lifecycle status
> events)**. Closed-source, folds into the SaaS. Visual design is the implementing agent's call.

> **REVISED 2026-07-11 (user — [ARCHITECTURE §16](./ARCHITECTURE.md), [contracts.md](./contracts.md)):**
> (a) the **VNC door is the VNC gateway** — auth is a short-lived, single-use **signed ticket
> minted by `apps/api`**, over **same-origin `/vnc`** (§6.2, DECIDED — cookie path dropped);
> (b) **no wake logic on the VNC path** — the pod shows three user-facing power states
> **off / sleep / on**; waking is a **MANUAL Start** (§6.4 superseded, §8); (c) new panel
> element: **tunnel connection status + Reconnect** (§5.1 item H); (d) hosted MCP rides the SaaS
> tunnel — no SPA work, but status copy may reference it.

Implementation spec for the **v1 dashboard pages** of the Anki-as-a-Service platform: the
per-user **instance control panel** and the embedded **noVNC** page. Written for an
implementing agent. Target repo: **`anki-mcp-saas`, `apps/web`**.

> **AnkiWeb re-login flow REMOVED in v1 (2026-07, platform decision).** The dashboard has
> **no AnkiWeb credential surface**: no `ReloginForm`, no `reloginAnkiWeb` mutation, no
> "credentials active within a minute" copy. The user signs into AnkiWeb **inside Anki, over
> the embedded VNC desktop**; the login persists on the instance's PVC and is wiped only when
> the instance is deleted ([`../REQUIREMENTS.md`](../REQUIREMENTS.md) Part B; ARCHITECTURE §6).
> §7 below is retained as the notice + the sync `auth_failed` guidance (now pointing at VNC).

**Authoritative inputs (do not contradict):**
[`ARCHITECTURE.md`](./ARCHITECTURE.md) §1, §2, §6, §9 (v1 scope), §10, §15; the shared
[`contracts.md`](./contracts.md) registry; the ci-buddy add-on spec
[`../REQUIREMENTS.md`](../REQUIREMENTS.md) (Part B — sync credentials removed in v1). The
**lifecycle-service API (`requirements-lifecycle-service.md`) now exists** — its §5 (API surface)
and §10 (status projection) are the authoritative shape for everything below; where this spec
still assumes, it is called out inline and collected in §13. Consume the lifecycle service; do not
invent it here.

> ⚠️ **Stack correction (verified 2026-07-10).** The dashboard is **NOT Next.js.** `apps/web`
> is a **Vite 8 + React 19 SPA** using **`react-router` 8**, **oRPC over a single
> authenticated WebSocket** (`@orpc-ws/*`), **TanStack Query**, **Tailwind v4**, **`sonner`**
> toasts, and the shared **`@repo/ui`** component library. Everything below fits those
> conventions with file citations. Ignore any "Next.js app router / API routes" mental model.

---

## 1. Goal & scope

Give a signed-in user a browser-only way to **see and control their own hosted Anki pod** and
**use it over VNC**, with no client install and no second credential.

**In scope (v1):**
1. **Instance panel** — status readout (user-facing power states **off / sleep / on** +
   transitional detail — DECIDED 2026-07-11, §8), on/off toggle, **manual Start** (wake from
   sleep), reset (confirmed), create / delete instance, **tunnel connection status + Reconnect**
   (§5.1 item H).
2. **Embedded noVNC page** — connect to the **VNC gateway's** websocket with an api-minted
   ticket (§6.2); **no auto-wake on this path** — the VNC *connection* requires the instance to
   be **on**; when asleep the page shows the sleep card with Start (§5.1.E, §6.4 step 3);
   reconnect; quality/scaling defaults; clipboard note.
3. ~~**AnkiWeb credentials (re-login) flow**~~ — **REMOVED in v1** (top banner): login happens
   inside Anki over VNC. The dashboard only surfaces sync `auth_failed` guidance (§7).
4. **AGPL "source code" link** (§15) on the footer / VNC page.

**Non-goals (v1 — explicitly out; ARCHITECTURE §9, decision 12):**
- ❌ **AnkiConnect** — only the AnkiMCP add-on. No AnkiConnect toggle, no API-key surface.
- ❌ **Per-user URLs / dedicated VNC domain / second credential** — identity routes off the
  session; one door.
- ❌ **Native VNC clients / raw-TCP VNC** — browser noVNC (a websocket) only.
- ❌ **Per-addon config UI** — no ci-buddy/add-on config toggles exposed to the user.
- ❌ Billing/quota changes, multi-instance per user, admin fleet views (those live elsewhere,
  e.g. `apps/admin-ui`).

---

## 2. Where this fits — stack & conventions to mirror

Cite and replicate these existing patterns. Do not introduce new libraries or a second
transport.

| Concern | Convention in `apps/web` | Reference file |
|---|---|---|
| Routing | `routes.tsx` array of `Route` objects (`path`, `element`, `title`, `requireAuth`, `fullBleed?`) | `apps/web/src/routes.tsx` |
| Auth / session | **cookie-BFF**: httpOnly `sid` cookie rides every request + the WS upgrade; browser holds **no tokens**; CSRF synchronizer token from `/api/auth/me` | `apps/web/src/lib/auth.ts` |
| WS client | one `<OrpcWs url={getWsUrl()}>` mounted by `AuthWsGate` after `me()` succeeds; consumers use `useAppWsClient()` | `apps/web/src/components/AuthWsGate/AuthWsGate.tsx`, `apps/web/src/hooks/useAppWsClient.ts` |
| Typed service client | `useXClient()` memoized over `client.rpc.<ns>.invoke({method,args})` (domain-blind gateway) | `apps/web/src/hooks/useMediaLibraryClient.ts` |
| Realtime | `useXEvents(onEvent)` over `useWsSubscription(client, rpc => rpc.<ns>.events(...))`; events are refetch hints, initial state fetched on `connected` | `apps/web/src/hooks/useMediaLibraryEvents.ts`, `apps/web/src/hooks/useTunnelStatus.ts` |
| Contract | oRPC + zod; loose `invoke({method:string,args:unknown})` + typed `events` on `appContract`; per-service methods in a `*-contract` package | `packages/orpc-contract/src/contract.ts`, `packages/media-library-contract/src/` |
| Gateway | `apps/api` stamps identity from the session, forwards over NATS req-reply, maps errors to `ORPCError` | `apps/api/src/api-gateway/websocket/router.ts` |
| Config / URLs | 3-tier getter: `window.__APP_CONFIG__` → `import.meta.env.VITE_*` → hostname derivation | `apps/web/src/lib/config.ts` |
| UI components | `@repo/ui`: `Switch`, `Dialog`, `Spinner`, `Toaster` (sonner) | `packages/ui/src/*` |
| Status chip | dot + pill, state→`{bg,text,label}` switch; **blue = healthy, not green** | `apps/web/src/components/ConnectionIndicator/ConnectionIndicator.tsx` |
| Page shell | title `text-2xl sm:text-3xl font-bold`, card `p-6 bg-white dark:bg-gray-900 border … rounded-xl`, alert `bg-amber-50 dark:bg-amber-950/20 … role="alert"` | `apps/web/src/pages/Home/home.tsx` |

**Two transports, keep them straight:**
- **Control plane** = the existing app WebSocket (`getWsUrl()` → `/ws`), oRPC. Instance
  status/toggle/reset/create/delete + the re-login form all ride this. Terminates at
  `apps/api`, which calls the **lifecycle service** over NATS (ARCHITECTURE §2).
- **Data plane** = a **separate** VNC websocket to the **VNC gateway** (2026-07-11 — the
  activator is shelved; ARCHITECTURE §16.3), reached **same-origin at `/vnc`**. noVNC's RFB
  connects to this. Do **not** try to tunnel VNC frames over the oRPC socket.

---

## 3. Data flow

```
Instance panel / re-login form
  └─ oRPC over /ws ─► apps/api (stamps keycloakId from sid session)
                        └─ NATS req-reply ─► lifecycle service ─► patch AnkiInstance CR
                                                                    (operator reconciles)

noVNC page (RFB module)                                        (revised 2026-07-11)
  ├─ oRPC over /ws ─► apps/api: mint a short-lived single-use VNC ticket (api owns the session)
  └─ wss same-origin /vnc (+ ticket) ─► VNC gateway ─► anki-<keycloakId>-0…:6080 (websockify)
       the gateway verifies the ticket locally (public key), resolves the pod by naming
       convention, and pipes bytes. NO auto-wake on this path — VNC connects only when "on";
       a sleeping pod is woken MANUALLY via Start (panel or the VNC page's sleep card, §6.4).
```

---

## 4. API contract consumed

The dashboard talks to the **lifecycle service** through the existing domain-blind gateway
seam. Add a `lifecycle` namespace to `appContract` mirroring `mediaLibrary`
(`packages/orpc-contract/src/contract.ts`):

```ts
lifecycle: {
  invoke: oc.input(z.object({ method: z.string(), args: z.unknown() })).output(z.unknown()),
  events: oc.output(z.custom<AsyncIterable<LifecycleEvent>>()),
},
```

Create a contract package **`packages/anki-lifecycle-contract/`** following the six-file layout
of `packages/media-library-contract/src/` (`model.ts`, `methods.ts`, `client.ts`,
`transport.ts`, `subjects.ts`, `index.ts`). Export `createLifecycleClient(invoke)` and a
`LifecycleClient` type; web consumes it via a new `apps/web/src/hooks/useLifecycleClient.ts`
(copy `useMediaLibraryClient.ts` verbatim, swapping `mediaLibrary` → `lifecycle`) and
`useLifecycleEvents.ts` (copy `useMediaLibraryEvents.ts`).

> ⚠️ **Reconcile method names with `requirements-lifecycle-service.md` §5.3 (now written).** The
> lifecycle service exposes contract methods `instance.create / status / start / stop / reset /
> delete / ankiwebLogin`; the dashboard names below map onto them (`getInstance` → `instance.status`,
> `createInstance` → `instance.create`, `deleteInstance` → `instance.delete`, `reloginAnkiWeb` →
> `instance.ankiwebLogin`, the rest 1:1). Use the lifecycle contract-package method strings when
> wiring `invoke({method, args})`; any remaining divergence is the lifecycle service's call.

| Method | Input | Output | Purpose |
|---|---|---|---|
| `getInstance` | — | `InstanceState \| null` | Current state; `null` = user has no instance yet |
| `createInstance` | — | `InstanceState` | Create the `AnkiInstance` CR (Provisioning) |
| `deleteInstance` | — | `{ ok: true }` | Delete CR + PVC per retention policy |
| `start` | — | `InstanceState` | **(revised 2026-07-11)** Power on **and wake**: lifecycle sets `spec.suspended: false` **and `spec.replicas: 1`** (single-writer v1) — the same call is the **manual wake from sleep**. ~~Never touches `spec.replicas` (activator-owned)~~ |
| `stop` | — | `InstanceState` | Set the power gate (lifecycle sets `spec.suspended: true`); ~~the activator stops waking and~~ the operator scales to 0 (the pod's `preStop` owns the graceful drain, ARCHITECTURE §5) |
| `reset` | — | `InstanceState` | Restart the pod (patch desired-state → reconcile; never live surgery, §9) |
| ~~`reloginAnkiWeb`~~ | — | — | **REMOVED in v1** (top banner) — no credential mutation exists; login is in-Anki over VNC (§7) |

`InstanceState` (interface shape proposed; the **status enum is canonical in
[contracts.md §11](./contracts.md)** — pinned 2026-07-11, do not diverge):

```ts
type InstanceStatus =        // canonical set: contracts.md §11 (2026-07-11)
  | "None"          // no CR (getInstance returned null → render create CTA)
  | "Provisioning"  // CR created, first-time storage/secret setup
  | "Stopped"       // power gate on (spec.suspended) — user-facing "off" (added 2026-07-11)
  | "Suspended"     // replicas 0, gate off, PVC retained — user-facing "sleep"
  | "Starting"      // 0→1 in progress — covers wake AND restart-in-progress (2026-07-11)
  | "Running"       // pod Ready — user-facing "on"
  | "Stopping"      // graceful downscale in flight
  | "Deleting"      // teardown saga running (added 2026-07-11 — contracts.md §11)
  | "Error";        // reconcile/attach/wake-timeout failure

interface InstanceState {
  status: InstanceStatus;
  desired: "on" | "off";        // last requested desired state
  ankiWebUsername?: string;     // display-only; from the last successful relogin
  lastSyncAuthFailed?: boolean; // surfaced to drive the re-login hint (§7.4)
  message?: string;             // human-readable error/support hint for Error state
  updatedAt: string;            // ISO timestamp
}
```

**Status mapping (lifecycle status API → UI status).** The lifecycle service derives status
from the k8s truth (CR + StatefulSet readiness) — "who's awake" is asked from Kubernetes. The
dashboard renders `status` verbatim; it must **not** compute readiness itself.

**User-facing power model (DECIDED 2026-07-11 — ARCHITECTURE §16.4).** The UI presents three
states: **off** (`Stopped` — power gate) / **sleep** (`Suspended` — replicas 0) / **on**
(`Running`); the transitional statuses (`Provisioning`/`Starting`/`Stopping`/`Deleting` —
2026-07-11, contracts.md §11) render as spinners
toward the target state. The internal enum above stays the wire detail; the lifecycle projection
(requirements-lifecycle-service §10) distinguishes `Stopped` (gate) from `Suspended` (idle) —
the dashboard must map them to **off vs sleep, never conflate them**. VNC is available only when
**on**; waking from sleep is a **manual Start** (§6.4).

**Realtime.** `useLifecycleEvents` events are payload-light hints; on each event and on WS
`connected`, refetch `getInstance()`. Follow the `useTunnelStatus.ts` reducer/`receivedForConnection`
pattern (initial fetch on connect, EventBus for live deltas, reconnect re-subscribe).

**Errors.** Gateway maps NATS failures to `ORPCError("SERVICE_UNAVAILABLE" | "TIMEOUT")` and
domain failures to `ORPCError("BAD_REQUEST", { data: { code } })` (see
`handleMediaLibraryInvoke`). The web client branches on `error.data.code` (e.g. `AUTH_FAILED`)
and shows a toast otherwise.

---

## 5. Pages & routes

Add to `apps/web/src/routes.tsx`:

```ts
{ path: "/instance",     element: <InstancePage />,  title: "Anki Instance", requireAuth: true },
{ path: "/instance/vnc", element: <VncPage />,       title: "Anki Desktop",  requireAuth: true, fullBleed: true },
```

Add a sidebar entry ("Anki Instance") in `apps/web/src/components/Sidebar` next to Studio.
File layout: `apps/web/src/pages/Instance/instance.tsx`, `.../Instance/vnc.tsx`, plus a
`components/` subdir (`InstanceStatusChip.tsx`, `PowerToggle.tsx`, `StartButton.tsx` (added
2026-07-11 — §5.1.B), `ResetButton.tsx`, `ReloginForm.tsx`, `VncViewport.tsx`,
`WakingOverlay.tsx`, `SourceCodeLink.tsx`).

### 5.1 Instance panel (`/instance`)

A single page composing:

**A. Status chip.** `InstanceStatusChip` mirrors `ConnectionIndicator` (dot + pill). Map (covers
every [contracts.md §11](./contracts.md) enum value — 2026-07-11):

| Status | Dot / pill | Label |
|---|---|---|
| Running | `bg-blue-500` | "On" (user-facing **on** — 2026-07-11) |
| Starting | `bg-yellow-500 animate-pulse` | "Starting…" |
| Provisioning | `bg-yellow-500 animate-pulse` | "Provisioning…" |
| Stopping | `bg-yellow-500 animate-pulse` | "Stopping…" |
| Deleting | `bg-gray-400 animate-pulse` | "Deleting…" (added 2026-07-11 — contracts.md §11) |
| Suspended | `bg-gray-400` | "Asleep" (user-facing **sleep** — 2026-07-11) |
| Stopped | `bg-gray-400` | "Off" (user-facing **off** — added 2026-07-11) |
| Error | `bg-red-500` | "Error" |
| None | (no chip; show create CTA) | — |

**B. On/off toggle.** `@repo/ui` `Switch`
(`{ checked, onChange, disabled?, label?, "aria-label"? }`):
- `checked = desired === "on"` (drive off **desired**, not transient status, so the toggle
  doesn't flip under the user during Starting/Stopping).
- `onChange(true)` → `start()`; `onChange(false)` → `stop()`. Optimistically set desired,
  reconcile from the next `getInstance`.
- `disabled` while `Provisioning | Starting | Stopping` or a mutation is in flight.
- Toast on success/failure via `sonner` (`toast.success/error`), matching `studio.tsx`.
- **Start button (added 2026-07-11 — the manual wake, §1/§8).** The toggle reflects
  `desired === "on"` while the instance is asleep, so it **cannot express a wake** — a separate
  **"Start" button** (`StartButton`) is shown when status is `Suspended` (sleep): it calls the
  same `start()` (which sets `suspended: false` + `replicas: 1` — idempotent), is `disabled`
  while a mutation is in flight, and the row's state copy reads e.g.
  "Asleep — Start to wake (~30–60s)". This is the panel-side manual wake ARCHITECTURE §16.4
  requires; the VNC page's sleep card reuses the same control (§6.4 step 3).

**C. Reset (with confirm).** `ResetButton` opens an `@repo/ui` `Dialog` (Radix; controlled via
`open` / `onOpenChange`). Content: `DialogTitle` "Reset your Anki?", `DialogDescription`
explaining it restarts the pod (in-progress edits are synced/persisted via the PVC; it is
**not** destructive to the collection). Confirm → `reset()`; disable the confirm button while
in flight; `DialogClose` cancels. Never a bare `window.confirm`.

**D. Create / delete instance.**
- When `getInstance()` is `null` (status `None`): render a primary CTA card "Create your Anki
  instance" → `createInstance()`; on success the page transitions to Provisioning via the
  status stream.
- Delete lives in a **danger zone** (`border-red-200 dark:border-red-800`) with its own confirm
  `Dialog` (type-to-confirm optional; at minimum an explicit destructive-styled confirm) →
  `deleteInstance()`. Copy must state the PVC is removed and data not synced to AnkiWeb is lost
  (ARCHITECTURE §4/§5 — sync is the cross-device source of truth; deletion drops the device).

**E. Open-in-VNC.** A prominent "Open Anki (VNC)" link/button → navigates to `/instance/vnc`.
**(revised 2026-07-11 — DECIDED, consistent with ARCHITECTURE §16.4 and §6.4)** Disabled **only**
when status is `Stopped` (off) or `None`; **enabled when `Suspended` (sleep)** — the VNC page
then shows the sleep card with the **Start** button (§6.4 step 3 stands); VNC *connects* only
when **on** (`Running`), and the VNC page itself performs **no wake** (§6.4).
~~disabled when off or asleep — from sleep the user presses Start first~~ ~~if Suspended, the
VNC page itself drives the wake~~

**F. AnkiWeb sign-in note.** No credential form (removed in v1 — top banner). Show static help
text: "Sign in to AnkiWeb inside Anki (open the remote desktop and click Sync). Your sign-in is
remembered until you delete the instance."

**G. Footer.** `SourceCodeLink` (§9).

**H. Tunnel connection + Reconnect (added 2026-07-11 — ARCHITECTURE §16.5).** Hosted MCP rides
the SaaS tunnel, so the panel shows the instance's **tunnel-connection state**
(connected / disconnected) from the existing tunnel-status seam (`useTunnelStatus.ts` pattern —
the tunnel already publishes per-user connection state; hosted connections are marked as such
server-side). When the pod is **on** but **disconnected** (e.g. kicked by a local client —
last-connected-wins; the hosted add-on never auto-reconnects), show a **Reconnect** button →
restart-based takeover: api → lifecycle bumps `spec.restartedAt` → pod recreates → the add-on
connects on startup, kicking any other client (the `instance.reset` lever, or a dedicated
`reconnect` method — the lifecycle contract's call; both bump `restartedAt`). Copy must say
reconnecting takes ~30–60s and will disconnect any other connected Anki client.

### 5.2 noVNC page (`/instance/vnc`)

Full-bleed (`fullBleed: true`). Renders `VncViewport` (the RFB host element) filling the
content area, a thin top bar (status chip + Reset + "Back to instance"), the sleep/off state
cards + post-Start `WakingOverlay` (§6.4 — no auto-wake, 2026-07-11), and the `SourceCodeLink`
in a corner (§9). See §6 for the embed.

---

## 6. noVNC embed

### 6.1 Embedding technique — RFB module import, NOT an iframe

Verified against current noVNC docs (`@novnc/novnc`, `core/rfb.js`, 2026-07-10). For an SPA
that owns its own chrome, session, and cold-start UX, **import the `RFB` class and drive it
from a React component** — do not embed `vnc.html` in an `<iframe>` (the iframe/query-string
mode is noVNC's standalone-application path; it can't share our session cookie handling or
render our overlay).

- Dependency: `@novnc/novnc` (add to `apps/web/package.json`). Import the ESM core module:
  `import RFB from "@novnc/novnc/core/rfb";`. Do **not** pull a heavy React wrapper; a thin
  `useEffect`-managed instance is enough and keeps us on the maintained upstream.
- `VncViewport` component:
  ```ts
  const containerRef = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<RFB | null>(null);
  useEffect(() => {
    if (!containerRef.current) return;
    const rfb = new RFB(containerRef.current, getVncWsUrl(), {
      // no credentials here — VNC password auth is disabled; the api-minted
      // ticket authenticates the upgrade (see §6.2). Set `wsProtocols` if the
      // VNC gateway negotiates a subprotocol.
    });
    rfb.scaleViewport = true;   // fit to container without a scrollbar
    rfb.resizeSession = false;  // v1: do NOT ask the server to resize (§6.5)
    rfb.qualityLevel = 6;       // 0–9; 6 is a sane default for text-heavy Anki
    rfb.compressionLevel = 2;
    rfb.focusOnClick = true;
    rfb.clipboardTarget = /* see §6.6 */;
    rfbRef.current = rfb;
    // addEventListener: "connect", "disconnect", "securityfailure", "clipboard"
    return () => { rfb.disconnect(); rfbRef.current = null; };
  }, [/* connection key, see §6.3/§6.4 */]);
  ```

### 6.2 VNC ticket auth (DECIDED 2026-07-11 — option 2 chosen; cookie path dropped)

**DECIDED 2026-07-11 (user — resolves contracts.md cross-cutting open decision #1):** auth on
the VNC websocket is a **short-lived, single-use, SIGNED VNC ticket**:

1. The dashboard calls a CSRF-authed mint on **`apps/api`** — which owns browser sessions, so
   the mint is NOT a lifecycle method (exact surface — oRPC method vs a small REST route — is
   implementation detail; ~~`lifecycle.getVncTicket()`~~ is superseded).
2. It opens the VNC websocket **same-origin at `/vnc`** (routed by the existing Cloudflare
   Tunnel ingress to the **VNC gateway** — no `vnc.` subdomain), passing the ticket (query param
   or WS subprotocol — the gateway's call; single-use + short TTL bounds URL-leak risk).
3. The **VNC gateway verifies the ticket locally with a public key** (no per-connect
   introspection call), resolves the pod by naming convention, and pipes bytes
   (ARCHITECTURE §16.3).

The former option (1) — cookie-on-upgrade — is **dropped**: same-origin routing dissolved the
cookie-domain/`SameSite` question, and the ticket keeps the gateway free of any session-store
coupling. VNC-password auth stays off. Never put the raw `sid` in a URL (unchanged).

### 6.3 Reconnect behavior

- Listen for the RFB `disconnect` event (`e.detail.clean` distinguishes intentional vs dropped).
- On an **unclean** disconnect while the user is on the page: show a non-blocking "Reconnecting…"
  banner and auto-retry with **capped exponential backoff** (e.g. 1s → 2s → 4s, cap ~15s, jitter),
  re-instantiating `RFB` (noVNC has no built-in reconnect for the library path — the app owns
  it; the query-string app's `reconnect` param does not apply here). Bump a `connectionKey`
  state to re-run the effect.
- Before reconnecting, refetch `getInstance()`: if the instance went to **sleep** (idle TTL
  fired while the tab was backgrounded) or **off**, STOP retrying and render the sleep/off
  state (§6.4) — do **not** auto-wake (2026-07-11: waking is manual).
- Each reconnect attempt needs a **fresh ticket** (single-use, §6.2) — re-mint before
  re-instantiating `RFB`.
- On a **clean** disconnect (user navigated away / clicked stop), do not retry.
- Give up after N attempts → render the Error state (§8) with a manual "Reconnect" button.

### 6.4 No auto-wake — manual Start only (SUPERSEDES the cold-start auto-wake, DECIDED 2026-07-11)

~~Previous design: on entering `/instance/vnc` with a `Suspended` instance, the page called
`start()` itself and polled to `Running`.~~ **Superseded (2026-07-11, user — ARCHITECTURE
§16.4): the VNC path has NO wake logic at all** — waking is a manual, explicit **Start**
(dashboard → api → lifecycle). Flow:

1. On entering `/instance/vnc`, fetch `getInstance()`.
2. **on** (`Running`) → mint a ticket (§6.2) and connect RFB immediately.
3. **sleep** (`Suspended`) → render a "Your Anki is asleep" card with a **Start** button (the
   same `start()` the panel uses). Do **NOT** call `start()` automatically.
4. **off** (`Stopped`) → render "Your Anki is turned off", pointing back to the instance panel's
   power toggle. No wake affordance beyond that.
5. **After the user presses Start** (or arrives during `Starting | Provisioning`): render
   `WakingOverlay` ("**Waking your Anki up…**", indeterminate `Spinner`) and follow the
   lifecycle status stream (`useLifecycleEvents` + `getInstance`) until `Running`, then connect.
   Bound the window (~60–90s — the platform wake budget, matching the tunnel's ensure-connected
   hold, ARCHITECTURE §16.5); on expiry show the wake-timeout Error state (§8) with a Retry
   button — never an infinite spinner.
6. A first RFB `disconnect` immediately after reaching `Running` (races websockify accept)
   triggers one silent retry (fresh ticket) before surfacing an error.

### 6.5 Quality & scaling defaults

- `scaleViewport = true` — the remote framebuffer scales to fit the viewport (no manual zoom in
  v1).
- `resizeSession = false` for v1 — do **not** request server-side resolution changes; the pod
  ships a fixed VNC geometry (keeps the image build simple; revisit later). Document as a known
  limitation.
- `qualityLevel = 6`, `compressionLevel = 2` — tuned for a text/UI-heavy desktop, not video.
  Do not expose these as user controls in v1.
- `viewOnly = false`.

### 6.6 Clipboard

- noVNC supports clipboard sync via the `clipboard` event (server→client) and
  `rfb.clipboardPasteFrom(text)` (client→server). Wire **server→local** (update the browser
  clipboard on the `clipboard` event, best-effort — subject to the browser Clipboard API
  permission/gesture rules) and **local→server** paste.
- Add a short helper note in the VNC page UI: browser clipboard access requires a user gesture
  and may be blocked by permissions; if paste-into-Anki fails, use the on-screen approach.
  Treat clipboard as **best-effort in v1**, not a guaranteed feature.

---

## 7. AnkiWeb sign-in (user-managed over VNC)

> **The re-login form/flow is REMOVED in v1 (2026-07, top banner).** There is no
> `ReloginForm`, no `reloginAnkiWeb` mutation, no password custody in the dashboard, and no
> "credentials active within a minute" copy. The user signs into AnkiWeb **inside Anki**:
> open the remote desktop (§6) and click the toolbar **Sync** button — Anki shows its stock
> login prompt. The sign-in persists on the instance's PVC (like desktop Anki) across
> restarts/sleep, and is wiped only when the instance is deleted.

### 7.1 Surfacing sync `auth_failed` guidance

When `getInstance().lastSyncAuthFailed` is true (the pod's MCP sync reported
`code="auth_failed"`, propagated control-plane-side), show
a **persistent alert** on the instance panel (amber alert block, `role="alert"`):
"Your AnkiWeb sign-in expired or was rejected. Sync is paused. Open the remote desktop and
sign in to AnkiWeb inside Anki (click Sync)." with a button that links to the VNC page (§6).
Clear the alert once a subsequent `getInstance` reports the flag false. Explain (help text)
that after signing in over VNC, the LLM's sync will auto-retry (the `auth_failed` payload
carries a retry hint).

---

## 8. Power states & error states — the state machine (revised 2026-07-11)

The instance panel and VNC page share one status source (§4). User-facing model = **off / sleep
/ on** (DECIDED 2026-07-11); transitional states render as spinners. Transitions the UI must
render:

```
None ──create──► Provisioning ──► sleep (Suspended) ◄──────────────────┐
                                     │ Start (MANUAL — panel button;      │ idle TTL
                                     │ or an MCP wake via tunnel→lifecycle,│ (lifecycle-owned)
                                     │ which the UI just renders)          │
                                     ▼                                    │
                                  Starting ──► on (Running) ──────────────┘
                                     │              │ stop (power gate)
                                     │              ▼
                                     │           Stopping ─► off (Stopped) ──Start──► Starting
                                     ▼              
                                   Error (wake timeout / reconcile failure; unclean VNC
                                          disconnect → reconnect, §6.3)
```

The VNC page never initiates a wake (§6.4). MCP-initiated wakes (tunnel → lifecycle,
ARCHITECTURE §16.5) surface as sleep→Starting→on transitions the UI simply renders via the
event stream.

**Error state rendering (both pages):**
- Status chip red "Error".
- Card/overlay with `InstanceState.message` (support hint from lifecycle service) and a
  **support link** (forum via `getForumUrl()` from `config.ts`, matching existing footer links).
- A **Retry** button: for a wake timeout → re-run the wake flow (§6.4 step 5 — after the user's
  Start); for a reconcile/attach error → `reset()`; for a create failure → `createInstance()`.
- Never leave the user on a bare spinner: a wake that exceeds the wake budget (§6.4) must
  resolve to Error, not spin forever.

**sleep → Starting → on in the UI:** the toggle reflects `desired` immediately (optimistic),
the status chip animates through Starting, the VNC page shows `WakingOverlay` until `Running`.
No user action beyond **pressing Start** is required (2026-07-11: opening the VNC page alone
never triggers a wake).

---

## 9. AGPL compliance element (§15)

ARCHITECTURE §15 requires a **visible "source code" link** for service users pointing to the
corresponding source of the shipped Anki build + baked add-ons (Anki is AGPL-3.0; users
interact over VNC/MCP → §13 network-clause obligation applies to Anki + image patches +
ci-buddy + AnkiMCP add-ons).

- Component `SourceCodeLink` rendering an anchor "Source code" → a **configurable URL**
  (`getSourceCodeUrl()` in `config.ts`, see §10) resolving to the repository/release page for
  the corresponding source.
- **Placement:** the **VNC page** (where the user directly interacts with the running Anki) —
  required — and the global dashboard footer. Must be visible without auth-gating beyond being
  in the app the user already uses.
- Keep the label stable and the link non-dead. It points at the **corresponding source** of the
  actually-shipped Anki build + add-ons, not a generic homepage. Coordinate the exact target
  with the headless-anki image build (which bakes the versioned add-ons).

---

## 10. Config / env

Extend `apps/web/src/lib/config.ts` following its exact 3-tier pattern (`window.__APP_CONFIG__`
→ `import.meta.env.VITE_*` → hostname derivation). Add to the `AppConfig` interface and add
getters:

| Getter | Config key / env | Derivation fallback | Use |
|---|---|---|---|
| `getVncWsUrl()` | `VNC_WS_URL` / `VITE_VNC_WS_URL` | **(revised 2026-07-11)** localhost dev port for the VNC gateway (assign in CLAUDE.md "Ports"); else **same-origin `wss://<window.location.host>/vnc`** — same-origin path routing, ~~`vnc.`-subdomain swap~~ no VNC subdomain (contracts.md open decision #1) | VNC gateway websocket (§6) |
| `getSourceCodeUrl()` | `SOURCE_CODE_URL` / `VITE_SOURCE_CODE_URL` | `https://github.com/…` (the corresponding-source repo/release) | AGPL link (§9) |

Mirror `getTunnelBaseUrl()`/`getStudioMcpUrl()` structure exactly (localhost branch + subdomain
swap + apex + hardcoded prod fallback). Also update the Docker `config.js`/envsubst template and
each app's `.env.example` (AGENTS.md "Configuration & Security Notes").

---

## 11. Testing

Follow repo conventions: unit/component tests are **co-located `*.spec.ts(x)`** (Vitest + jsdom
+ `@testing-library/react`, globals on, setup `apps/web/src/test/setup.ts`); E2E is Playwright
under `tests/e2e/workflows` using the Page Object Model and real Keycloak fixtures (AGENTS.md;
`apps/web/vitest.config.ts`; `tests/e2e/playwright.config.ts`).

### 11.1 Component / hook tests (`apps/web/src`)

Mock the transport at the library boundary — `vi.mock("@orpc-ws/react", …)` returning a fake
`useOrpcWs`/`useWsSubscription` (pattern from
`apps/web/src/hooks/useQuotaSubscription.spec.ts`). Do not hit the network. Cover:

1. `InstanceStatusChip` renders correct label/dot class for each `InstanceStatus`.
2. `PowerToggle`: `onChange(true)` → calls `start()`; `onChange(false)` → `stop()`; disabled
   during `Starting|Stopping|Provisioning` and while a mutation is pending; reflects `desired`.
3. `ResetButton`: opens the confirm `Dialog`; confirm calls `reset()`; cancel/close does not.
4. Create CTA shows only when `getInstance()` is `null`; delete lives in the danger zone with
   its own confirm.
5. `auth_failed` guidance alert appears when `lastSyncAuthFailed` is true, links to the VNC
   page, and clears when false. (No `ReloginForm` tests — the form was removed in v1.)
6. `VncViewport`: a ticket is minted (via the `apps/api` seam) **before** RFB is constructed
   with the same-origin `getVncWsUrl()`; `disconnect()` called on unmount; unclean `disconnect`
   triggers the backoff/reconnect path with a **fresh ticket per attempt** (mock the `RFB`
   class); a wake exceeding the wake budget resolves to Error, not an infinite spinner.
8. **No auto-wake (revised 2026-07-11):** `Suspended` (sleep) on mount → sleep card shown,
   **`start()` NOT called automatically**, RFB not constructed; pressing **Start** → `start()`
   called once + `WakingOverlay` until `Running`; `Stopped` (off) on mount → off notice, no
   Start-triggered wake from the VNC page beyond the pointer to the panel.
9. **Tunnel Reconnect (added 2026-07-11):** disconnected-while-on shows the Reconnect button;
   clicking it calls the restart lever exactly once and surfaces the ~30–60s takeover copy.
10. **`StartButton` (added 2026-07-11 — §5.1.B):** rendered only when status is `Suspended`;
    clicking it calls `start()` exactly once; disabled while a mutation is in flight; the
    "Asleep — Start to wake (~30–60s)" state copy is shown.

Mock `@novnc/novnc/core/rfb` (a class with `disconnect`, `addEventListener`, and settable
`scaleViewport`/`qualityLevel`/… props) so no real VNC server is needed.

### 11.2 E2E happy path (`tests/e2e/workflows/anki-instance.spec.ts`)

Add a page object `tests/e2e/pages/instance.page.ts` (semantic selectors — roles /
`aria-labelledby`, matching `dashboard.page.ts`; no `data-testid`). Happy path
**create → start → VNC connect → stop**:

1. Authenticated user (existing Keycloak device-flow fixture) navigates to `/instance`.
2. No instance → click "Create" → status reaches Provisioning then sleep/on.
3. Press **Start** on the panel (the §5.1.B `StartButton`, shown while asleep) → status reaches
   Running (**not** by opening the VNC page — 2026-07-11, no auto-wake).
4. Open `/instance/vnc` (only once on) → assert the RFB canvas/viewport becomes visible (a
   stubbed/fake websockify may be needed in the E2E environment — see §13; at minimum assert a
   ticket mint + a connect attempt against the same-origin `getVncWsUrl()`).
5. Toggle off → status reaches Stopped (off); assert the VNC button disables.
6. Assert the "Source code" link is present on the VNC page and points at
   `getSourceCodeUrl()`.

If a live pod/websockify is unavailable in CI, assert up to the VNC **connection attempt**
(spy on the WS URL) and document the manual VNC check, rather than claiming coverage the harness
can't provide.

---

## 12. Acceptance checklist

- [ ] `/instance` and `/instance/vnc` routes added (`vnc` full-bleed) + sidebar entry.
- [ ] `lifecycle` namespace on `appContract`; `packages/anki-lifecycle-contract` created;
      `useLifecycleClient`/`useLifecycleEvents` hooks copy the media-library pattern.
- [ ] Status chip maps all `InstanceStatus` values (incl. `Stopped` = off vs `Suspended` =
      sleep — never conflated); blue = Running (not green).
- [ ] On/off `Switch` drives `desired`, calls `start`/`stop`, disabled during transitions;
      **Start is the only wake** (manual — 2026-07-11).
- [ ] Tunnel connection state shown on the panel; **Reconnect** button (restart-based takeover,
      ~30–60s copy) appears only when on-but-disconnected.
- [ ] Reset uses a confirm `Dialog` (no `window.confirm`); create CTA on `None`; delete in a
      danger zone with its own confirm and data-loss copy.
- [ ] noVNC embedded via **RFB module import** (not an iframe); `scaleViewport`, `qualityLevel=6`,
      `compressionLevel=2`, `resizeSession=false`.
- [ ] VNC WS auth = **api-minted short-lived single-use signed ticket** over **same-origin
      `/vnc`** (DECIDED 2026-07-11); cookie path dropped; the raw `sid` never in a URL.
- [ ] **No auto-wake on the VNC path** (2026-07-11): sleep → sleep card + manual Start; off →
      disabled/notice; waking overlay only after Start, bounded (~60–90s) → Error (never an
      infinite spinner).
- [ ] Reconnect with capped backoff on unclean disconnect (fresh ticket per attempt);
      re-checks status and stops (no auto-wake) if the instance went to sleep/off.
- [ ] Clipboard wired best-effort with a user-facing note.
- [ ] No AnkiWeb credential surface anywhere (removed in v1 — top banner); the §5.1.F static
      sign-in-over-VNC help text is present.
- [ ] `auth_failed` guidance alert surfaces and clears from `lastSyncAuthFailed`, pointing at
      the VNC page (§7.1).
- [ ] Error state shows lifecycle `message` + support link + contextual Retry.
- [ ] AGPL "Source code" link on the VNC page and footer → `getSourceCodeUrl()`.
- [ ] `config.ts` getters + `AppConfig` + `config.js`/`.env.example` updated for `VNC_WS_URL`
      and `SOURCE_CODE_URL`.
- [ ] Component tests (§11.1) + E2E happy path (§11.2) pass; no non-goal (AnkiConnect,
      per-addon config, native VNC) leaked into the UI.

---

## 13. Open questions

1. ~~**Lifecycle-service API is unwritten.**~~ **RESOLVED:** `requirements-lifecycle-service.md`
   now exists — its **§5 (API surface)** and **§10 (status projection)** are authoritative for the
   method surface, `InstanceState` shape, status enum, and event stream. Reconcile the method
   *names* per §4's callout; the lifecycle service owns these shapes.
2. ~~**VNC websocket session carriage (§6.2).**~~ **RESOLVED (2026-07-11, user —
   [contracts.md → Cross-cutting open decisions #1](./contracts.md)):** a short-lived,
   single-use, **signed VNC ticket minted by `apps/api`**, verified locally by the **VNC
   gateway** (public key), over **same-origin `/vnc`** — the cookie-domain question dissolved
   with the same-origin routing; the cookie path is dropped (§6.2).
3. ~~**Activator wake/hold status endpoint (§6.4).**~~ **MOOT (2026-07-11):** the activator is
   shelved — there is no `/instance/status` endpoint. Wake progress = the lifecycle status
   stream (`useLifecycleEvents` + `instance.status`), which §6.4 already uses.
4. ~~**Hold timeout value (§6.4/§8).**~~ ~~**RESOLVED:** the activator's `HOLD_TIMEOUT` default
   (60s).~~ **Re-scoped (2026-07-11):** the activator's hold is gone and the VNC path has no
   hold at all (no wake); the bounded window in §6.4 step 5 is the **wake budget ~60–90s**,
   aligned with the tunnel's ensure-connected hold (ARCHITECTURE §16.5).
5. **AGPL source-code target (§9).** Exact URL for the corresponding source of the shipped Anki
   build + baked add-ons (public repo/release vs a per-version artifact) — coordinate with
   headless-anki and the §15 monorepo public/private open decision.
6. **Dev port for the VNC WS** (§10) — still open; the endpoint is now the **VNC gateway's**
   (2026-07-11 — its placement, own app vs endpoint on an existing service, is an implementation
   detail). Assign in `anki-mcp-saas` CLAUDE.md "Ports" (tunnel 3004, studio 3006, media-library
   3008, sandbox 3009 are taken).
7. **CSRF-mutation wrapper** — `apps/web/src/lib/auth.ts` intentionally does not re-export
   `authClient.mutate`/`getCsrfToken` (no authed mutation exists yet). `reloginAnkiWeb` is the
   first; confirm it routes through the oRPC gateway (preferred, consistent with everything
   else) vs a REST `authClient.mutate` call, and re-export the wrapper accordingly.
8. **E2E VNC fidelity** (§11.2) — whether CI can run a real/fake websockify+pod, or E2E stops at
   the connection attempt. Affects how much of "VNC connect" the happy path can assert.
