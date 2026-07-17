# requirements-operator — AnkiInstance operator

Implementation spec for the **AnkiInstance Kubernetes operator** of the Anki-as-a-Service
platform. Written for an implementing agent. This doc is subordinate to
[ARCHITECTURE.md](./ARCHITECTURE.md) (esp. §2, §3, §6, §8, §10, §12, §13) and to the tunnel
credentials contract in [contracts.md §8c](./contracts.md) — where this doc is silent
or unclear, those win. Anything the authoritative docs leave unresolved is parked in
[§12 Open questions](#12-open-questions), not decided here by fiat.

Lives at `operator/` in the platform monorepo ([ARCHITECTURE §14](./ARCHITECTURE.md)). Shared
cross-component values (GVR, field ownership, naming, namespace, labels, ports, Secret) are fixed
in [contracts.md](./contracts.md) — this doc restates them for context and must match it.

**Naming (see [contracts.md §3](./contracts.md)):** the CR `metadata.name` **is the keycloakId**
(a DNS-1123-safe UUID) and `spec.user` carries the same value; the operator names each child
**`anki-<keycloakId>`** (StatefulSet, headless Service, and the credentials Secret it mounts).
Examples below use `alice` purely as a stand-in for that keycloakId string.

**Version anchor (verify at scaffold time — do not spec against stale APIs):** the current
Kubebuilder line targets **Kubernetes 1.36 / Go 1.26**, ships **controller-runtime v0.24.x** and
**controller-tools v0.21.x**. `ReadWriteOncePod` (RWOP) is **GA since k8s 1.29** and is
**CSI-only**. `persistentVolumeClaimRetentionPolicy` on StatefulSets is stable. Cilium on-cluster
is **1.19.4** (`cilium.io/v2` `CiliumNetworkPolicy`, `toFQDNs` supported). Re-check each of these
against the versions actually installed before writing manifests.

---

## 1. Goal & scope

Reconcile one `AnkiInstance` custom resource (**one per user**) into the running k8s objects that
make up that user's per-user Anki runtime unit ([ARCHITECTURE §3](./ARCHITECTURE.md)):
a **StatefulSet** (+ its per-user PVC via `volumeClaimTemplate`), a **headless Service**, and the
**mount** of that user's credentials Secret. The operator is a **dumb, self-healing reconciler**:
it translates declared CR intent into children and keeps them converged. It never sees live user
traffic and never decides *when* a user should be awake.

**In scope**
- The `AnkiInstance` CRD (types, status subresource, printer columns, defaulting, minimal
  validation).
- Reconciling the per-CR children listed above, with owner references and correct storage /
  security / affinity settings.
- A **fleet image rollout** mechanism: moving thousands of per-user StatefulSets onto a new pod
  image at a controlled pace (§7).
- Standard operator plumbing: leader election, metrics, health probes, minimal RBAC.

**Out of scope (other components — do NOT build here)**
- **Deciding replicas / idle TTL / wake / drain.** ~~The **activator** owns all of that
  ([ARCHITECTURE §7](./ARCHITECTURE.md)); it patches `spec.replicas` and the operator obeys.~~
  **(2026-07-11: the activator is shelved for v1 — the lifecycle service now writes
  `spec.replicas`, [ARCHITECTURE §16](./ARCHITECTURE.md).)** Still not the operator's job: it
  obeys whatever `spec.replicas` says; the pod's `preStop` owns the drain.
- **Creating / deleting CRs, writing spec (except replicas), the credential Secret's *content*
  (the tunnel token — the former `user→pass→hkey` exchange was removed in v1, ARCHITECTURE §6).**
  The **lifecycle service** owns those
  ([ARCHITECTURE §2, §6](./ARCHITECTURE.md)). The operator only *mounts* the Secret; it never
  reads it and holds no RBAC on it (§6, §9).
- **The pod image itself** (Anki + ci-buddy + AnkiMCP + rclone + noVNC), rclone/FUSE privilege
  model, storage-class / CSI install, node labelling, the namespace-wide network policy object —
  all **headless-anki / infrastructure** repos ([ARCHITECTURE §13](./ARCHITECTURE.md)). This doc
  states the *contract* the operator depends on and flags anything it needs from them.

---

## 2. Division of authority (hard contract — the operator's prime directive)

This is the load-bearing invariant of the whole control plane
([ARCHITECTURE §2, §8](./ARCHITECTURE.md)).

> **SUPERSEDED FOR v1 (2026-07-11, user — [contracts.md §2](./contracts.md),
> [ARCHITECTURE §16](./ARCHITECTURE.md)):** the activator is shelved; **the lifecycle service is
> the only spec writer** — create/delete the CR and all of `spec` (`user`, `suspended`,
> `replicas`, `restartedAt`, the data-fate field, `image`). **The operator's own row is
> unchanged**: `status` only (+ its finalizer, §5.4), never any `spec` field, and it owns the
> children. The two-writer table below is retained verbatim for the activator's possible
> revival.

Historical two-writer contract:

| Actor | May write on the CR | Must never touch |
|---|---|---|
| **lifecycle service** | create / delete the CR; all of `spec` **except `spec.replicas`** (i.e. `user`, `suspended`, future toggles, image pin) | `spec.replicas`; anything under `status`; the children |
| **activator** | **only `spec.replicas`** (0↔1) | every other `spec` field; `status`; the children |
| **operator** | **only `status`** (+ finalizer if one is used) | any `spec` field — it must **never** flip `spec.replicas`, `suspended`, etc. on its own |

- The operator **owns everything below the CR** (StatefulSet, Service, and the Secret *mount*).
  It creates/updates/deletes those; nobody else should.
- The operator computes the child StatefulSet's `replicas` from CR spec (§4.3). Setting a
  *StatefulSet's* replicas is not "flipping replicas" — the forbidden act is mutating the CR's
  `spec.replicas` field, which belongs to the spec writer (lifecycle in v1; the activator's
  exclusive signal under the historical contract).
- Enforce the split with **field ownership** (Server-Side Apply field managers, §5.3) and, if
  desired, a validating admission policy later. v1 relies on the contract + SSA field managers;
  it does not ship an admission webhook to police writers (flagged §12).

---

## 3. The `AnkiInstance` CRD

- **Group / version:** `anki.ankimcp.ai/v1alpha1` (kind `AnkiInstance`, singular `ankiinstance`,
  plural `ankiinstances`, shortName `anki`). Namespaced (all instances live in the
  **`anki-instances`** namespace — [contracts.md §4](./contracts.md)).
- **Scope:** namespaced. Name convention: the CR name **is the keycloakId**
  (`metadata.name == spec.user`, both the keycloakId); validate they match. A Keycloak `sub` is a
  UUID and therefore already DNS-label-safe (RFC 1123); the CRD validates the label form
  defensively. Children are named **`anki-<keycloakId>`** — see §4.

### 3.1 `spec`

```yaml
spec:
  user: alice          # string, REQUIRED, IMMUTABLE. Identity of the tenant.
  replicas: 0          # int32, enum {0,1}, default 0. LIFECYCLE-OWNED (2026-07-11 single-writer
                       #   v1; activator-owned if revived). 0 = asleep, 1 = awake.
  suspended: false     # bool, default false. LIFECYCLE-OWNED administrative gate (dashboard on/off).
  restartedAt: ""      # string (RFC3339), optional. LIFECYCLE-OWNED (added 2026-07-11 — ARCH §16.6).
                       #   Restart nonce: the operator copies the value into the pod template as an
                       #   annotation (the `kubectl rollout restart` pattern) → pod recreated, PVC
                       #   untouched. Used for tunnel reconnect/takeover. (Formerly also hkey
                       #   pickup — credential channel removed in v1, ARCHITECTURE §6.)
  dataRetention: Retain # enum {Retain, Delete}, default Retain (PROPOSED name — this spec owns the
                       #   exact naming, contracts.md §2). LIFECYCLE-OWNED (added 2026-07-11 — ARCH
                       #   §16.7). PVC fate on CR delete: Delete → the operator's finalizer deletes
                       #   the PVC; Retain → PVC survives (default, fail-safe).
  # ---- future toggle placeholders (present in schema, inert in v1) ----
  ankiConnect: false   # bool, default false. Reserved: gate the AnkiConnect surface. No-op in v1.
  image: ""            # string, optional. Per-instance image PIN / override (canary a single user).
                       #   Empty => use the operator's fleet image (§7). LIFECYCLE-OWNED.
```

- **`user`** — immutable (enforce with a CEL validation rule
  `self == oldSelf`; on current k8s, `x-kubernetes-validations` transition rules). Immutability
  matters: `user` seeds the PVC / Secret / B2-prefix identity; changing it would strand a disk.
- **`replicas`** — constrained to `{0,1}` (a per-user unit is a single pod;
  [ARCHITECTURE §3, decision 3](./ARCHITECTURE.md) — RWOP structurally forbids 2 pods on the disk).
  Enum via CEL `self in [0,1]`. ~~**Activator-owned** (§2).~~ **Lifecycle-owned (2026-07-11
  single-writer v1, §2).**
- **`restartedAt`** (added 2026-07-11) — RFC3339 string; when set/changed the operator stamps it
  as a pod-template annotation (e.g. `anki.ankimcp.ai/restartedAt`), which changes the template
  hash → the StatefulSet controller recreates the pod; the PVC is untouched (`whenScaled/
  whenDeleted: Retain`). Empty/absent = no annotation. This is the platform's only restart
  mechanism (no live pod surgery).
- **`dataRetention`** (added 2026-07-11; proposed name — this spec owns it) — governs the
  operator's **deletion finalizer** (§5.4): on CR delete with `Delete`, the operator deletes the
  per-user PVC before removing its finalizer; with `Retain` (default) the PVC survives. This is
  what keeps the lifecycle service's RBAC free of workload-object verbs
  ([contracts.md §2](./contracts.md)).
- **`suspended` vs `replicas` — the decision (task item 1).** *Idle suspend/resume is modelled as
  `replicas` 0↔1*, per the closed architecture ([decisions 3 & 7](./ARCHITECTURE.md): the activator
  is the only actor that sees traffic and owns the wake/idle flip). We do **not** overload a
  boolean to mean "asleep." **But** the dashboard's administrative *on/off*
  ([ARCHITECTURE §9](./ARCHITECTURE.md)) is a *different intent* owned by the *lifecycle service*,
  and the hard contract (§2) forbids the lifecycle service from writing `replicas` (the activator's
  field). So a **second, orthogonal** field `suspended` carries admin-off. The operator's
  **effective desired replicas = `suspended ? 0 : replicas`** (§4.3). Rationale for two fields
  rather than one:
  - Two authorities must never share a field, or they race and clobber (§2). `replicas` (activator,
    high-frequency, traffic-driven) and `suspended` (lifecycle, rare, human-driven) are genuinely
    different signals with different owners.
  - Folding admin-off into `replicas` would force the lifecycle service to write the activator's
    field — a contract violation — and the next traffic event would silently un-suspend the user.
  - The alternative "admin-off = delete the CR" is heavier (STS GC + PVC-retain + later
    recreate/rebind) and conflates *disable* with *deprovision*. The architecture doesn't pin the
    dashboard-off→CR mapping, so this is **flagged in [§12](#12-open-questions)**; `suspended` is
    the recommended, contract-safe design.
  - **(2026-07-11)** Under the single-writer v1 contract the write-race half of this rationale is
    historical, but the two fields stay: **off** (gate) and **sleep** (replicas) are distinct
    user-facing states ([ARCHITECTURE §16.4](./ARCHITECTURE.md)).

### 3.2 `status` (status subresource enabled)

```yaml
status:
  phase: Running                 # enum: Provisioning | Suspended | Starting | Running | Error
  observedGeneration: 7          # int64: .metadata.generation last reconciled
  replicas: 1                    # int32: STS .status.readyReplicas (mirrored for kubectl/printers)
  currentImage: repo/anki:25.9.2-3  # string: image the live/last-applied pod template carries
  conditions:                    # []metav1.Condition, standard shape
    - type: Ready                #   pod is Running & Ready (or, when suspended, "AsIntended")
    - type: Progressing          #   a child change is rolling out
    - type: Degraded             #   a reconcile/child error is latched here
```

- **`phase`** (a coarse, human-facing summary; conditions are the machine-facing truth):
  - `Provisioning` — CR seen, children not yet all created / never yet ready.
  - `Suspended` — effective desired replicas 0 (idle **or** admin-off); STS scaled to 0, PVC
    retained. This is a *healthy* resting state, not an error.
  - `Starting` — effective desired replicas 1, pod not yet Ready (wake in progress).
  - `Running` — pod Ready.
  - `Error` — a child failed to apply, or a wedged state (e.g. multi-attach) the operator surfaces
    but cannot itself resolve.
- **`conditions`** — use `meta.SetStatusCondition`; every condition carries
  `observedGeneration`, `reason`, `message`, `lastTransitionTime`. `Ready=True` when the pod is
  Ready; `Ready=True` with reason `Suspended` is acceptable when suspend is the *intended* state
  (a suspended instance is not "unready" — it's correctly off). Pick one convention and keep it
  consistent; recommended: `Ready` reflects "the instance is in the state its spec asks for."
- **`observedGeneration`** — set to `.metadata.generation` at the end of each successful reconcile
  so callers can tell "has the operator caught up with my last spec write?" (the lifecycle service
  — and the activator, if revived — polls this).
- Status is only ever written by the operator (§2), via the status subresource (so a status write
  never bumps `metadata.generation`).

### 3.3 Printer columns (`+kubebuilder:printcolumn`)

| Name | JSONPath | Notes |
|---|---|---|
| `USER` | `.spec.user` | |
| `DESIRED` | `.spec.replicas` | wake/sleep intent (lifecycle-written in v1) |
| `SUSPENDED` | `.spec.suspended` | admin gate |
| `PHASE` | `.status.phase` | |
| `READY` | `.status.replicas` | ready pods (0/1) |
| `IMAGE` | `.status.currentImage` | for eyeballing a fleet rollout |
| `AGE` | `.metadata.creationTimestamp` | `type: date` |

### 3.4 Defaulting & validation

- Prefer **declarative CEL validation** (`x-kubernetes-validations`) and schema defaults over a
  mutating/validating **webhook** — no webhook = no cert plumbing, no availability coupling, one
  less thing racing at bootstrap ([ARCHITECTURE §12 item 11](./ARCHITECTURE.md): apps must tolerate
  racing dependencies). Rules: `replicas in [0,1]`; `user` immutable; `metadata.name == spec.user`.
- If a webhook becomes necessary (e.g. to *enforce* the §2 writer split server-side), add it later
  behind cert-manager; call it out rather than smuggle it into v1.

---

## 4. Reconciled children (per CR, with ownerRefs)

For a CR named `alice` (= keycloakId) the operator ensures exactly these objects — each named
**`anki-alice`** (`anki-<keycloakId>`) — all with a controller `ownerReference` back to the CR
(`SetControllerReference` → `controller: true`, `blockOwnerDeletion: true`) so they
garbage-collect when the CR is deleted.

Common labels on every child (and used as pod selector), per [contracts.md §5](./contracts.md):
`app.kubernetes.io/name=anki-instance`, `app.kubernetes.io/managed-by=anki-operator`,
`app.kubernetes.io/instance=anki-alice`, `anki.ankimcp.ai/user=alice`. (These are the reconciled
shared values; they must match the namespace-wide network policy's `endpointSelector`, §4.4.)

### 4.1 StatefulSet (the core child)

Name = `anki-<CR name>` (e.g. `anki-alice`). Key fields:

```yaml
apiVersion: apps/v1
kind: StatefulSet
spec:
  serviceName: anki-alice            # the headless Service (§4.2) — governs pod DNS identity
  replicas: <effective, §4.3>
  podManagementPolicy: OrderedReady  # single replica, ordering is moot; either is fine
  updateStrategy: { type: RollingUpdate }
  revisionHistoryLimit: 1            # thousands of STSes — keep controller-revision clutter down
  selector: { matchLabels: <common labels> }
  persistentVolumeClaimRetentionPolicy:
    whenDeleted: Retain              # explicit even though it's the default
    whenScaled:  Retain              # LOAD-BEARING: Delete would wipe the disk every idle cycle
  volumeClaimTemplates:
    - metadata: { name: profile }
      spec:
        accessModes: ["ReadWriteOncePod"]      # RWOP — structurally blocks a 2nd pod on the WAL DB
        storageClassName: hcloud-volumes       # Hetzner Cloud Volume via hcloud-csi (CONFIG, §6)
        resources: { requests: { storage: 10Gi } }  # profile SQLite + rclone vfs cache (CONFIG)
  template:
    metadata:
      labels: <common labels>
    spec:
      automountServiceAccountToken: false      # §10 layer 2 — pod carries NO k8s creds
      serviceAccountName: anki-pod             # a zero-permission SA (no RoleBindings)
      securityContext:                         # POD-level; §10 layer 3
        runAsNonRoot: true
        runAsUser:  <anki uid, image-defined>
        fsGroup:    <anki gid>                 # so the 0400 Secret file is readable (§4.5)
        seccompProfile: { type: RuntimeDefault }
      affinity:
        nodeAffinity:                          # §3 — Cloud Volumes can't attach to dedicated servers
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: ankimcp.ai/storage    # contracts.md §6 — infra must create this node label
                    operator: In
                    values: ["hcloud-volumes"]
      terminationGracePeriodSeconds: <config; covers graceful quit + rclone drain, ARCH §5>
      containers:
        - name: anki
          image: <fleet image or spec.image override, §7>
          securityContext:                     # CONTAINER-level; §10 layer 3
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: <true if the image tolerates it; else false + note>
            capabilities: { drop: ["ALL"] }
          resources:
            requests: { cpu: "…", memory: "…" }   # noisy-neighbor control on shared CX43 nodes
            limits:   { cpu: "…", memory: "…" }    #   (§10 layer 3, capacity §12 item 10)
          ports:
            - { name: mcp,    containerPort: 3141 }   # container port only — NOT a Service port
                                                      #   in v1 (2026-07-11, contracts.md §7)
            - { name: vnc-ws, containerPort: 6080 }   # noVNC websocket (websockify)
          volumeMounts:
            - { name: profile, mountPath: /data }             # the PVC
            - { name: credentials, mountPath: /run/ankimcp, readOnly: true }  # §4.5 (tunnel token)
          # readiness probe: gates the VNC door + wake completion — probe reflects "Anki up +
          # rclone mount live + add-on live" (image defines the exact check; 2026-07-11: the
          # platform-level MCP readiness signal is "add-on connected to the tunnel", not this
          # probe — contracts.md §7).
      volumes:
        - name: credentials
          secret:
            secretName: anki-alice              # anki-<keycloakId> (contracts.md §8); lifecycle writes it
            optional: true                       # missing Secret must NOT wedge pod start (§4.5)
            defaultMode: 0400                    # 0400, owned by anki uid (via fsGroup) — §4.5
```

**Pod shape — DECIDED (2026-07-10): per-user rclone SIDECAR** (default
`--media-mount-mode=sidecar`; [ARCHITECTURE §10 layer 3](./ARCHITECTURE.md),
[contracts.md §10](./contracts.md)). The template above shows the **anki container**, which stays
**unprivileged**: it inherits the pod securityContext (`runAsNonRoot`, `seccompProfile`), sets
`allowPrivilegeEscalation:false` + drop-`ALL`, **adds no capabilities**, and runs with env
`MEDIA_MOUNT_MODE=external` (it never runs rclone). In `sidecar` mode the operator ALSO renders:

- a shared **`media` `emptyDir`** at `/media/b2`, mounted `HostToContainer` in the anki container and
  **`Bidirectional`** in the sidecar;
- a **native rclone sidecar** (init container, `restartPolicy: Always`) running the same image with
  `CONTAINER_ROLE=rclone-sidecar`. Its container-level securityContext is **`privileged: true`** —
  the honest reason: Bidirectional mount propagation (needed so the FUSE mount reaches the anki
  container) **requires** a privileged container in Kubernetes; `SYS_ADMIN`+`/dev/fuse` alone are not
  accepted (kubernetes/kubernetes PR #117812, closed unmerged). The sidecar still runs as the non-root
  anki uid and holds no k8s creds. It mounts the profile PVC (`subPath: .rclone-cache`) for the VFS
  cache-on-PVC durability (§4-ARCHITECTURE) and gets its B2 key via `envFrom` the per-user Secret
  ([contracts.md §9](./contracts.md)). **This makes the `anki-instances` namespace PSA-`privileged`**
  (infra-owned label — [contracts.md §4](./contracts.md)); the namespace holds only Anki pods and the
  default-deny CiliumNetworkPolicy walls them in.

**PSA note.** With `--media-mount-mode=external` (CSI-provided mount) the operator renders only the
anki container and no sidecar, so the pod is PSA-`baseline`-clean and the namespace could stay
`baseline`. The **default is `sidecar`** (the decided design), so the shipped default namespace label
is `privileged`. Both container templates apply the layer-3 hardening (`runAsNonRoot`, drop-`ALL`,
`seccompProfile`, resource limits); only the sidecar's mandatory `privileged` breaks *baseline*.

### 4.2 Headless Service

Name = `anki-<CR name>` (e.g. `anki-alice`); `clusterIP: None`; selector = common labels; ports:
**`vnc-ws` (6080) only in v1** (2026-07-11 — ~~`mcp` (3141) +~~ `mcp` is not a Service port:
nothing dials inbound MCP, it rides the add-on's outbound tunnel connection, contracts.md §7).
It backs the StatefulSet's `serviceName` (stable pod DNS
`anki-alice-0.anki-alice.anki-instances.svc.cluster.local`). The
~~activator~~ **VNC gateway** (2026-07-11) **resolves user→pod by this naming convention**
(routing is a naming convention, not stored state) and forwards to the pod; ingress to the pod is
allowed only from the VNC gateway by the network policy (§4.4). A headless (not ClusterIP) Service is
the right fit: single backend pod with a stable identity, no need for kube-proxy VIP load-balancing
across a 1-pod set. (~~If the activator turns out to prefer a stable VIP, a ClusterIP Service is a
drop-in swap — flagged §12.~~ 2026-07-11: resolved — the VNC gateway dials the stable pod DNS;
headless stays. §12.5.)

### 4.3 Effective replicas (how the operator computes STS `.spec.replicas`)

```
effectiveReplicas = (spec.suspended == true) ? 0 : spec.replicas
```

- The operator sets the STS replicas to this value and **never writes back to the CR's
  `spec.replicas`** (§2). When `suspended` flips true, the STS scales to 0 regardless of what the
  spec writer has put in `replicas`; when `suspended` returns false, the STS returns to whatever
  `replicas` currently is (so an admin re-enable doesn't force a wake — it just stops forcing sleep).
- `whenScaled: Retain` guarantees the PVC survives every 1→0 transition
  ([ARCHITECTURE §3, decision 3](./ARCHITECTURE.md)).

### 4.4 Network policy — **namespace-wide, not per-CR** (task decision + justification)

**Decision: one namespace-wide `CiliumNetworkPolicy` (default-deny + a single shared allow-list),
NOT one policy per instance.** It is **not a per-CR reconciled child**; it is a **namespace
singleton** managed as a **static manifest in the infrastructure/argocd app**
([ARCHITECTURE §10 layer 1, §13](./ARCHITECTURE.md): "CiliumNetworkPolicy objects (first in the
cluster)" are infra-owned). Justification:

- Every Anki pod has **identical** L3/L4 needs: ingress only from the ~~activator~~ **VNC
  gateway** (2026-07-11); egress only to DNS + AnkiWeb + the B2 endpoint **+ the in-cluster
  tunnel service** (2026-07-11 — the add-on's outbound WS, contracts.md §4/§8c). A per-user
  policy would be thousands of byte-identical objects —
  pure churn for the operator to reconcile and for Cilium to compile into identities, with **zero**
  isolation benefit (a namespace-wide default-deny already denies pod↔pod, *including between two
  Anki pods*, because no rule permits it).
- **Per-user data isolation is not a network concern here** — it's handled at
  [layer 2](./ARCHITECTURE.md) by the per-user, prefix-scoped **B2 key**, not by L3 policy. So
  per-instance network granularity buys nothing the shared policy doesn't already give.
- Keeping it out of the operator also keeps **CiliumNetworkPolicy RBAC out of the operator** (§9,
  least privilege) and keeps the policy in the infra repo alongside the namespace and PSA labels
  where the security team owns it.

The operator's only obligation: **label pods** so the namespace policy's `endpointSelector` can
select them (§4 common labels). Reference policy shape (adapt hosts/labels; `cilium.io/v2`):

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata: { name: anki-default, namespace: anki-instances }
spec:
  endpointSelector:
    matchLabels: { app.kubernetes.io/name: anki-instance }   # all Anki pods; default-deny otherwise
  ingress:
    - fromEndpoints:
        - matchLabels:
            # VNC gateway identity (2026-07-11 — replaces the activator) — its pod labels,
            # cross-namespace via k8s:io.kubernetes.pod.namespace
            app.kubernetes.io/name: vnc-gateway   # adapt to the gateway's actual labels
      # (only the vnc-ws port 6080 — nothing dials mcp 3141 in v1)
  egress:
    # 1) DNS to kube-dns, WITH L7 DNS visibility so toFQDNs can learn the IPs
    - toEndpoints:
        - matchLabels: { k8s-app: kube-dns }
      toPorts:
        - ports: [{ port: "53", protocol: UDP }, { port: "53", protocol: TCP }]
          rules: { dns: [ { matchPattern: "*" } ] }
    # 2) AnkiWeb sync (FQDN-based, not brittle IPs — ARCH §10 layer 1)
    - toFQDNs:
        - matchPattern: "*.ankiweb.net"
        - matchPattern: "*.ankiuser.net"
    # 3) Backblaze B2 endpoint
    - toFQDNs:
        - matchName: "s3.eu-central-003.backblazeb2.com"
    # 4) in-cluster tunnel service (2026-07-11 — the AnkiMCP add-on's outbound WS to /connect;
    #    contracts.md §4/§8c) — select the tunnel's pods cross-namespace
    - toEndpoints:
        - matchLabels:
            app.kubernetes.io/name: tunnel        # adapt to the tunnel's actual labels/namespace
```

- The **DNS visibility rule (egress #1) is mandatory** for `toFQDNs` to work — Cilium learns
  IPs by proxying the DNS response; without an allowed, DNS-inspected path to kube-dns, FQDN rules
  match nothing. This is the single most common way FQDN policies silently break.
- **Explicitly denied by omission** (the whole point): k8s API, Hetzner metadata
  `169.254.169.254`, Vault/`pg-shared`/NATS/Keycloak/ArgoCD/etc., and pod↔pod
  ([ARCHITECTURE §10 layer 1](./ARCHITECTURE.md)). Verify with **Hubble** that the policy bites.
- The operator **may optionally** reconcile the *existence* of this singleton (self-healing) if the
  team prefers, but v1 leaves it to argocd. Flagged §12.

### 4.5 Credentials Secret **mount** (the operator mounts; it never writes)

The operator wires the STS to mount a per-user Secret whose **content is written by the lifecycle
service** ([ARCHITECTURE §6](./ARCHITECTURE.md)).

> **Scope change (2026-07): sync credentials removed in v1.** The Secret's former
> `sync-credentials.json` data key (the hkey channel for ci-buddy) is **gone** — AnkiWeb login is
> user-managed over VNC and persists on the PVC ([../REQUIREMENTS.md](../REQUIREMENTS.md) Part B).
> The Secret now carries only the **`tunnel-credentials.json`** data key
> ([contracts.md §8c](./contracts.md)), read by the AnkiMCP add-on. The earlier layered notes
> (kubelet-refresh + ci-buddy file-poll rotation, then restart-based pickup via
> `spec.restartedAt`, [ARCHITECTURE §16.6](./ARCHITECTURE.md)) are all moot.

Contract the mount must satisfy:

- **Whole-volume mount, NEVER `subPath`.** `subPath` mounts are frozen at creation and never
  refresh — that would silently kill token rotation. Mount the Secret volume at a directory
  (`/run/ankimcp`) so the file `/run/ankimcp/tunnel-credentials.json` refreshes.
- **`defaultMode: 0400` + `fsGroup`** so the file is `0400` yet readable by the Anki uid (a
  root-owned 0400 mount is unreadable by the unprivileged app).
- **`optional: true`** so a not-yet-created Secret doesn't wedge pod start. Lifecycle should
  create Secret + CR together, but the pod must not hard-depend on ordering.
- The operator has **no RBAC to read Secrets** (§9) — it only names one in a volume; the kubelet
  performs the mount. It never touches the token.

### 4.6 What the operator does **not** create

- **The PVC** — created by the STS from `volumeClaimTemplate`; the operator must **not** put its own
  ownerRef on the PVC (that would GC the disk on CR delete). PVC lifecycle is the StatefulSet
  controller's job, governed by `persistentVolumeClaimRetentionPolicy` (§4.1). CR delete → STS
  GC'd → with `whenDeleted: Retain` the STS controller leaves the PVC (never adds an owner to it),
  so **the disk survives** even a CR deletion. **(2026-07-11 delta:** the operator never *creates*
  the PVC, but it now **deletes** it in exactly one case — the deletion finalizer when
  `spec.dataRetention: Delete`, §5.4. Deprovisioning intent still comes from the lifecycle
  service, expressed as the CR field, never as a direct lifecycle PVC verb.**)**
- **The credentials Secret** (lifecycle, §4.5), the **network policy** (infra, §4.4), the
  **StorageClass / CSI / node labels** (infra, [§13](./ARCHITECTURE.md)).

---

## 5. Reconcile mechanism

### 5.1 Controller wiring

- `Owns(&appsv1.StatefulSet{})`, `Owns(&corev1.Service{})`, `For(&AnkiInstance{})` so child status
  changes (pod becomes Ready, STS rollout progresses) re-trigger reconcile and update CR status.
- Optionally `Watches` the fleet-config ConfigMap (§7) mapped to enqueue **all** CRs on change.
- Use `SetupWithManager` with a sane `MaxConcurrentReconciles` (fleet is large; tune, e.g. 5–10)
  and rate limiting.

### 5.2 Reconcile flow (idempotent, level-triggered)

1. Get the `AnkiInstance`; if not found, return (GC handles children via ownerRefs — see §5.4 on
   finalizers).
2. Compute the **desired** Service + StatefulSet from spec (§4), including effective replicas
   (§4.3), the resolved image (§7), and the `restartedAt` pod-template annotation (§3.1 —
   value copied verbatim; a change rolls the pod).
3. **Apply** each child (§5.3), setting the controller ownerRef.
4. Read child status → compute `phase` + conditions + `currentImage` + `replicas` (§3.2) and write
   the **status subresource** (only status; never spec).
5. Set `observedGeneration = metadata.generation`.
6. Requeue only if needed (e.g. mid-rollout budget wait, §7; or a transient error → return err for
   backoff). Steady state is watch-driven, not poll-driven.

### 5.3 Server-Side Apply (preferred over CreateOrUpdate)

Reconcile children with **Server-Side Apply**: `client.Patch(ctx, obj, client.Apply, …)` with a
stable **`client.FieldOwner("anki-operator")`** and `client.ForceOwnership`. SSA gives per-field
ownership and sidesteps the read-modify-write races that `controllerutil.CreateOrUpdate` hits under
heavy concurrency (thousands of objects). It also plays correctly with argocd's
`ServerSideApply=true` ([ARCHITECTURE §12 item 11](./ARCHITECTURE.md)): distinct field managers
mean argocd and the operator co-own an object without thrash. **Verify controller-runtime's current
SSA helper API at scaffold time** (the typed-apply / `client.Apply` surface has been evolving);
`controllerutil.CreateOrUpdate` is an acceptable fallback if the SSA ergonomics aren't ready, but
SSA is the target.

### 5.4 Deletion / finalizers

> **SUPERSEDED (2026-07-11 — [ARCHITECTURE §16.7](./ARCHITECTURE.md), contracts.md §2): v1 DOES
> ship an operator finalizer**, for the PVC data-fate flow.

- CR delete with `dataRetention: Retain` (default): ownerReference GC — STS + Service collected;
  PVC retained by `whenScaled/whenDeleted: Retain` (§4.6).
- CR delete with `dataRetention: Delete`: the operator's **finalizer** deletes the per-user PVC
  (idempotent; after the STS/pod is gone) before removing itself. This is how the lifecycle
  service's delete saga destroys the disk **without** holding any workload-object RBAC
  (requirements-lifecycle-service §5.6). Keep the finalizer minimal and idempotent; B2
  deprovisioning stays a lifecycle-service action (never the operator's).

---

## 6. Config surface

Operator-level configuration (not per-CR). Prefer a small **ConfigMap** (`anki-operator-config`,
watched) and/or flags; keep secrets out (there are none — the operator holds no credentials).

| Key | Meaning | Default (proposal) |
|---|---|---|
| `fleetImage` | pod image all instances converge to unless `spec.image` overrides (§7) | *(required)* |
| `rollout.onlyWhenSuspended` | only re-template a STS while it's at 0 replicas (§7) | `true` |
| `rollout.maxConcurrent` | max STSes allowed out-of-date **and** actively rolling at once (§7) | small, e.g. `20` |
| `rollout.paused` | freeze all image rollout | `false` |
| `storageClassName` | PVC storage class | `hcloud-volumes` |
| `pvcSize` | per-user PVC request | `10Gi` |
| `nodeLabelKey` / `nodeLabelValue` | node-affinity target (§4.1, contracts.md §6) | `ankimcp.ai/storage` / `hcloud-volumes` |
| `mediaMountMode` (`--media-mount-mode`) | `sidecar` (per-user privileged rclone sidecar; §4.1) or `external` (CSI-provided) | `sidecar` |
| `namespace` | instance namespace | `anki-instances` |
| resource requests/limits, grace period | pod sizing (§4.1) | tune per capacity §12 item 10 |
| sidecar resource requests/limits | rclone sidecar sizing (`sidecar` mode; rclone is light, VFS cache on disk) | `50m`/`500m` CPU, `128Mi`/`512Mi` mem |

CRD-level config lives on each CR's `spec` (§3.1). Anything security-sensitive (the network policy,
storage class install, node labels) is **infrastructure-owned**, not operator config.

---

## 7. Fleet image rollout (task item 4)

**Problem:** roll a new pod image across **thousands** of per-user StatefulSets without (a) a
thundering herd of simultaneous restarts and (b) disrupting users who are actively working.

**Design — operator-driven, suspend-aware, budgeted:**

1. **Target image = operator config `fleetImage`** (§6). A per-CR `spec.image` **override** wins
   (canary/pin a single user, [ARCHITECTURE §8](./ARCHITECTURE.md) "image/version pin"). Changing
   `fleetImage` enqueues all CRs.
2. **Resolve each STS's image** = `spec.image` if set, else `fleetImage`. If the live STS pod
   template already carries the resolved image, nothing to do.
3. **Suspend-aware application (`rollout.onlyWhenSuspended`, default true):**
   - If the instance is **suspended** (effective replicas 0), just patch the template image now —
     **nothing rolls** (0 pods); the *next wake* brings the pod up already on the new image. Zero
     disruption.
   - If the instance is **awake** (replicas 1), **defer** the template change (re-queue) rather
     than restart an active user mid-session. Because the ~~activator~~ **lifecycle service**
     (2026-07-11) suspends every idle user within the ~1h TTL
     ([ARCHITECTURE §1](./ARCHITECTURE.md)), **the fleet naturally drains onto the new
     image within roughly one TTL window, with no forced interruptions.** (Provide an escape hatch —
     `onlyWhenSuspended: false` — for urgent security rebuilds that must roll awake pods too.)
4. **Global pacing (`rollout.maxConcurrent`):** even suspended-only re-templating can cause a herd
   when many users wake at once onto a fresh image (image pulls, cold rclone caches). Cap the number
   of instances that are simultaneously **out-of-date-and-rolling**: a rollout-aware pass lists
   managed STSes, counts how many are mid-transition (image != target and not yet Ready on the new
   revision), and only advances new instances toward target up to the budget; the rest re-queue.
   This keeps node/image-pull and B2-egress pressure bounded on the 3-node cluster
   ([capacity §12 item 10](./ARCHITECTURE.md)).
5. **`rollout.paused`** freezes all of the above (hold a bad image).
6. **Observability:** expose rollout progress as metrics (§8) — gauges for
   `instances_total`, `instances_on_target_image`, `instances_rolling` — and mirror the applied
   image per CR in `status.currentImage` (§3.2) for `kubectl get`.

Implementation note: the "count mid-transition across the fleet" step needs a fleet-wide view.
Either a periodic reconcile that lists STSes and computes the budget, or a shared in-memory counter
guarded by the leader. Keep it simple and leader-scoped (single active operator, §8).

---

## 8. Scaffolding, RBAC, plumbing (task item 5)

- **Framework:** Go + **Kubebuilder** (verify current version; targets k8s 1.36 / Go 1.26 /
  controller-runtime v0.24.x at time of writing). Standard layout under `operator/`:
  `api/v1alpha1/` (types + zz_generated deepcopy), `internal/controller/`, `config/` (Kustomize:
  `crd/`, `rbac/`, `manager/`, `default/`), `cmd/main.go`, `Makefile`, `Dockerfile`. Use
  `+kubebuilder:` markers for CRD gen, printer columns, RBAC, and status subresource.
- **Leader election:** enable (`--leader-elect`), Lease-based, in the operator's own namespace — a
  single active reconciler so the fleet-rollout budget (§7) and status writes have one writer.
- **Metrics:** controller-runtime's `/metrics` (workqueue depth, reconcile latency/errors) **plus**
  custom fleet gauges (§7). Serve behind the standard auth-proxy/`--metrics-secure` per current
  Kubebuilder scaffolding.
- **Health/readiness:** `/healthz` + `/readyz` (`--health-probe-bind-address`).
- **Minimal RBAC (verify verbs against generated markers).** Least privilege — note the deliberate
  *absences*:

  | Resource (apiGroup) | Verbs | Why |
  |---|---|---|
  | `ankiinstances` (`anki.ankimcp.ai`) | get, list, watch, update, patch | reconcile input |
  | `ankiinstances/status` | get, update, patch | write status (§3.2) |
  | `ankiinstances/finalizers` | update | the deletion finalizer (§5.4 — required since 2026-07-11) |
  | `statefulsets` (`apps`) | get, list, watch, create, update, patch, delete | the core child |
  | `services` (core) | get, list, watch, create, update, patch, delete | headless Service |
  | `persistentvolumeclaims` (core) | get, list, watch, **delete** | **(2026-07-11)** delete added for the data-fate finalizer flow **only** (§5.4); the operator still never *creates* PVCs (§4.6) |
  | `events` (core) | create, patch | surface reconcile events |
  | `configmaps` (core) | get, list, watch | fleet config (§6), if used |
  | `leases` (`coordination.k8s.io`) | get, list, watch, create, update, patch, delete | leader election |
  | **`secrets`** | **NONE** | the operator must **never** read user credentials (§4.5, §10 layer 2). Mounting a Secret needs no operator RBAC. |
  | **`ciliumnetworkpolicies`** | **NONE** | namespace-wide policy is infra-owned (§4.4) |

  Scope Role vs ClusterRole to the instance namespace where possible; leader-election Lease lives in
  the operator's namespace.

---

## 9. Deployment (argocd app-of-apps, task item 6)

Per [ARCHITECTURE §13 + §12 item 11](./ARCHITECTURE.md):

- Package as an argocd app under the app-of-apps (`argocd/apps/anki-operator/` + a tier entry);
  charts published to Harbor OCI like the `ankimcp` umbrella.
- **CRDs deploy in an early tier**, ahead of the operator and of any lifecycle-service/activator
  apps that create `AnkiInstance` objects. Ship CRDs as their own app/manifest set (not bundled
  behind the operator Deployment) so they land first.
- **`ServerSideApply=true`** on the argocd app — required because CRDs are large (full schema) and
  because SSA field managers let argocd and the operator co-own objects without fighting (§5.3).
- **Tolerate racing dependencies — no sync-wave ordering.** Sync waves are currently non-functional
  on this cluster (all apps apply at once, converge by retry). The operator must therefore:
  - start and **retry cleanly** if its CRD isn't registered yet (controller-runtime's cache sync
    will error and back off — fine; don't crash-loop hard, let it converge);
  - not assume the namespace / network policy / storage class exist at first reconcile — reconcile
    is level-triggered and idempotent, so a child that can't be created yet just retries.
- The namespace-wide CiliumNetworkPolicy, PSA `baseline` namespace label, StorageClass/CSI, and
  node labels are **separate infra apps** ([§13](./ARCHITECTURE.md)); the operator app declares no
  ordering on them.

---

## 10. Security summary (maps to ARCHITECTURE §10)

- **Layer 1 (network):** operator labels pods for the namespace-wide default-deny policy (§4.4);
  ingress only from the ~~activator~~ **VNC gateway**, egress only DNS + AnkiWeb + B2 **+ the
  in-cluster tunnel service** (2026-07-11, contracts.md §4).
- **Layer 2 (minimal pod secrets):** `automountServiceAccountToken:false`, zero-permission SA, no
  operator Secret RBAC; only the tunnel token reaches the pod as a mounted file, plus the
  per-user prefix-scoped B2 key (delivered by infra/lifecycle, not the operator). (The hkey is
  no longer delivered — credential channel removed in v1, ARCHITECTURE §6; it exists on the pod
  only inside `prefs21.db` after the user's own VNC login.)
- **Layer 3 (pod hardening):** the **anki container** is hardened (`runAsNonRoot`, drop-`ALL`,
  no-privesc, seccomp `RuntimeDefault`, no added caps, resource limits, §4.1). The FUSE/`baseline`
  tension is **resolved (2026-07-10): per-user rclone sidecar** — the sidecar is `privileged: true`
  (Bidirectional propagation requires it), confined to its own container, so the namespace is
  PSA-`privileged`; the anki container stays unprivileged. `--media-mount-mode=external` keeps a
  baseline-clean CSI path available.
- **Layer 4 (honest limit):** shared kernel / control-plane-colocated nodes — documented in the
  architecture, unchanged by the operator.

---

## 11. Testing (task item 7)

### 11.1 envtest reconcile tests (apiserver + etcd; **no kubelet, no GC, no CSI**)

envtest runs a real kube-apiserver but **not** the kube-controller-manager or a kubelet, so it can
assert what the *operator* writes, but **cannot** actually schedule pods, bind PVCs, or run
ownerRef GC. Write tests to that boundary:

1. **Create → children exist:** apply a CR; assert a StatefulSet and headless Service appear with
   correct names, labels, ownerRef (controller=true), RWOP access mode, `whenScaled/whenDeleted:
   Retain`, node affinity, `automountServiceAccountToken:false`, the Secret volume (whole-volume,
   `optional:true`, `defaultMode:0400`), and effective replicas.
2. **Replicas flip:** patch `spec.replicas` 0→1→0; assert STS `.spec.replicas` tracks it; assert
   the operator never writes back to `spec.replicas`.
3. **Suspend gate:** set `spec.suspended=true` with `spec.replicas=1`; assert effective STS
   replicas 0. Clear it; assert STS returns to 1.
4. **PVC retention is *declared*:** assert the STS carries `whenScaled: Retain` (the *actual*
   "PVC survives 1→0" behavior needs a real StatefulSet controller — §11.2). Guards against a
   regression that flips the policy to Delete.
5. **Status:** assert `phase`/conditions/`observedGeneration`/`currentImage` transition correctly
   as child status is faked (Provisioning→Starting→Running; →Suspended when replicas 0; →Error on a
   forced apply failure).
6. **ownerRefs for GC:** assert children carry the controller ownerRef (actual GC on CR delete
   needs the real garbage collector — §11.2).
7. **Image rollout pacing:** pre-create N STSes at various images/replica/readiness states; run the
   rollout pass; assert (a) suspended instances get re-templated, (b) awake instances are deferred
   when `onlyWhenSuspended`, (c) no more than `maxConcurrent` are advanced per pass, (d) `paused`
   freezes everything, (e) a `spec.image` override beats `fleetImage`.
8. **Immutability / validation:** `user` change rejected; `replicas: 2` rejected;
   `name != user` rejected.
8b. **Restart field (added 2026-07-11):** setting/changing `spec.restartedAt` stamps the pod
   template annotation (template hash changes → rollout); an unchanged value is a no-op;
   clearing it does not roll the pod spuriously. The PVC spec is untouched.
8c. **Data fate (added 2026-07-11):** with `dataRetention: Delete`, CR delete drives the
   finalizer to delete the PVC then release; with `Retain` (default) the finalizer releases
   without touching the PVC. (Actual PVC-object deletion needs a real cluster — §11.2; envtest
   asserts the operator's delete call / finalizer sequencing.)
9. **Sidecar pod shape (`--media-mount-mode=sidecar`, default):** assert a native rclone sidecar
   (init container, `restartPolicy: Always`, `privileged: true`, no `allowPrivilegeEscalation:false`,
   `CONTAINER_ROLE=rclone-sidecar`, `envFrom` the per-user Secret optional); the shared `media`
   `emptyDir` mounted **`Bidirectional`** in the sidecar and **`HostToContainer`** in the anki
   container; the cache-on-PVC subPath mount; the anki container `MEDIA_MOUNT_MODE=external`, **no
   added caps**, not privileged; both containers carry the drain `preStop`. In `external` mode: no
   sidecar, no shared media volume, single unprivileged container.

Pure-function unit tests (no apiserver): effective-replicas, image resolution, budget math,
phase/condition computation, **and `desiredStatefulSet` in both media-mount modes**
(`statefulset_test.go`) + `Config.Validate` rejecting a bad `--media-mount-mode`.

### 11.2 Real-cluster tests (kind + Cilium, or the actual cluster with hcloud-csi)

Things envtest structurally can't cover:

- **Actual PVC retention** across 1→0→1 (real STS controller honoring `whenScaled: Retain`; data
  persists across the cycle) — needs kind + a CSI (or the real cluster). RWOP enforcement (second
  pod blocked) is **CSI-only**, so it needs a real CSI driver.
- **ownerRef GC** on CR delete (children removed, **PVC retained**) — needs the real garbage
  collector.
- **Node affinity** actually pins pods to `cloud`-labelled nodes.
- **Network policy** bites: with the namespace policy applied, verify (Hubble) ingress-from-
  VNC-gateway-only and egress limited to DNS/AnkiWeb/B2 + the in-cluster tunnel service
  (2026-07-11); k8s API / metadata / Vault blocked.
- **Secret mount refresh:** update the Secret; confirm the mounted file refreshes within kubelet's
  ~60s (validates the whole-volume, non-`subPath` mount).
- **PSA baseline:** the pod actually admits under the `baseline`-enforced namespace.

---

## 12. Acceptance checklist

- [ ] `AnkiInstance` CRD installs (`anki.ankimcp.ai/v1alpha1`), status subresource + printer columns
      present; `user` immutable, `replicas∈{0,1}`, `name==user` enforced (CEL, no webhook).
- [ ] Creating a CR yields a StatefulSet + headless Service with correct ownerRefs, RWOP,
      `whenScaled/whenDeleted: Retain`, node affinity to `cloud` nodes, PSA-baseline + layer-3
      hardened pod template, `automountServiceAccountToken:false`, resource limits.
- [ ] Credentials Secret (tunnel token only — §4.5) mounted whole-volume (never `subPath`),
      `0400`+`fsGroup`, `optional:true`, at `/run/ankimcp`; operator holds **no** Secret RBAC.
- [ ] `spec.replicas` 0↔1 drives STS replicas; `spec.suspended` gates to 0; operator never writes
      any `spec` field (only `status`).
- [ ] **(2026-07-11)** `spec.restartedAt` changes roll the pod via the template annotation
      (rollout-restart pattern), PVC untouched; unchanged value = no-op.
- [ ] PVC survives 1→0 (whenScaled Retain) and survives CR delete with `dataRetention: Retain`
      (whenDeleted Retain); with `Delete` the operator's finalizer removes the PVC; children GC
      on CR delete; operator puts no ownerRef on the PVC.
- [ ] Fleet image rollout: `fleetImage` change rolls the fleet suspend-aware, budgeted by
      `maxConcurrent`, `paused`-able, `spec.image` override wins; progress visible in metrics +
      `status.currentImage`.
- [ ] Namespace-wide CiliumNetworkPolicy (infra-owned) selects Anki pods via operator-set labels;
      operator creates no per-CR policy and has no Cilium RBAC.
- [ ] Leader election on; metrics (incl. fleet gauges) + health probes served; minimal RBAC matches
      §8 (no secrets, no cilium).
- [ ] argocd app: CRDs in an early tier, `ServerSideApply=true`, converges without sync-wave
      ordering / tolerates racing deps.
- [ ] Tests: envtest cases §11.1 pass; real-cluster checks §11.2 documented (and run where a
      cluster is available).

---

## Open questions

Unresolved or under-specified in the authoritative docs — surfaced here rather than decided.

1. ~~**Dashboard on/off → CR mapping.**~~ **RESOLVED (2026-07-10, [contracts.md §2](./contracts.md)):**
   the power gate is the lifecycle-owned `spec.suspended` bool (contract-safe vs the activator's
   `replicas`, §3.1/§4.3); the operator computes `effectiveReplicas = suspended ? 0 : replicas`.
   The activator never writes `suspended`; the lifecycle service never writes `replicas`. (The
   "dashboard-off = delete the CR" alternative was rejected as heavier and conflating disable with
   deprovision.)
2. **Enforcing the writer split server-side.** §2 is a *contract*, enforced in v1 only by
   convention + SSA field managers. Is a validating admission webhook (activator may patch only
   `replicas`; lifecycle may not) wanted, accepting the cert-manager/bootstrap-racing cost it adds?
   Deferred here. **(2026-07-11: lower stakes now — one external spec writer exists in v1, so
   there is no split to police until the activator returns.)**
3. **Network policy ownership.** This doc puts the namespace-wide `CiliumNetworkPolicy` in the infra
   repo (§4.4), matching [§13](./ARCHITECTURE.md). Should the operator instead *reconcile the
   singleton's existence* for self-healing (at the cost of Cilium RBAC on the operator)? Currently:
   no.
4. ~~**rclone/FUSE privilege model.**~~ **DECIDED 2026-07-10: per-user rclone SIDECAR** (default
   `--media-mount-mode=sidecar`; [ARCHITECTURE §10 layer 3](./ARCHITECTURE.md),
   [contracts.md §10](./contracts.md)). The operator renders the two-container shape in §4.1: the
   anki container unprivileged, a **`privileged`** rclone sidecar (Bidirectional propagation requires
   privileged — PR #117812 closed unmerged), shared `media` emptyDir with `Bidirectional`/`HostToContainer`
   propagation, cache-on-PVC subPath, B2 key via `envFrom`. The namespace is PSA-`privileged`.
   `--media-mount-mode=external` remains for a CSI-provided mount (single unprivileged container).
5. ~~**Activator → pod addressing.**~~ **RESOLVED-by-pivot (2026-07-11):** the activator is
   shelved; the **VNC gateway** dials the stable pod DNS
   (`anki-<keycloakId>-0.anki-<keycloakId>…:6080`) by naming convention — the **headless**
   Service stays; no ClusterIP VIP needed.
6. ~~**Finalizer / deprovisioning (§5.4).**~~ **RESOLVED (2026-07-11 —
   [ARCHITECTURE §16.7](./ARCHITECTURE.md)):** the operator ships a deletion **finalizer** driven
   by the CR data-fate field (`dataRetention`, §3.1/§5.4): `Delete` → operator deletes the PVC;
   `Retain` (default) → data survives. PVC `delete` RBAC added (§8, data-fate flow only); B2
   deprovisioning stays a lifecycle-service action.
7. ~~**Exact label/StorageClass/node-label/Secret-name keys.**~~ **RESOLVED
   ([contracts.md §§3–8](./contracts.md)):** node label `ankimcp.ai/storage: hcloud-volumes`,
   StorageClass `hcloud-volumes`, Secret `anki-<keycloakId>`, common labels per §5, ports `mcp`
   (3141) / `vnc-ws` (6080), namespace `anki-instances`. The `ankimcp.ai/storage` node label must
   still be **created by infra** on the Cloud-Volume nodes (it does not exist today).
8. **`Ready` condition semantics while suspended.** §3.2 recommends "`Ready` = instance is in the
   state its spec asks for" (so a correctly-suspended instance is `Ready`, not "unready"). Confirm
   this reads well for the dashboard/lifecycle consumers (2026-07-11: the activator consumer is
   shelved), or invert to "`Ready` = pod serving."
