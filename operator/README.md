# AnkiInstance operator

Kubernetes operator that reconciles one `AnkiInstance` custom resource (one per
user) into that user's per-user Anki runtime unit: a **StatefulSet** (+ its per-user
PVC via `volumeClaimTemplate`), a **headless Service**, and the **mount** of the
user's credentials Secret. It is a dumb, self-healing reconciler — it translates
declared CR intent into children and keeps them converged. It never sees live user
traffic and never decides when a user should be awake.

Spec: [`../docs/requirements-operator.md`](../docs/requirements-operator.md).
Shared contract: [`../docs/contracts.md`](../docs/contracts.md) (this wins on any
conflict). Architecture: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

## Tooling

Targets the kubebuilder line for **Kubernetes 1.36 / Go 1.26**:
controller-runtime **v0.24.x**, controller-tools **v0.21.x**, k8s.io/* **v0.36**.
`controller-gen` / `setup-envtest` are run via `go run` (no global install). Go's
auto toolchain (`GOTOOLCHAIN=auto`) fetches Go 1.26 if your local Go is older.

> **CI must run with `GOTOOLCHAIN=auto`** (or provision Go ≥ 1.26). `go.mod` pins
> `go 1.26.0` because controller-runtime v0.24.1 requires ≥ 1.26; a runner with an
> older Go and `GOTOOLCHAIN=local` will hard-fail the build at the toolchain floor.
> The `Makefile` already exports `GOTOOLCHAIN ?= auto`; ensure CI does not override it.

## Division of authority (the prime directive)

Two writers touch a CR; they must never fight over a field (docs/contracts.md §2):

| Actor | Writes on the CR | Field manager |
|---|---|---|
| **lifecycle** | `spec.user`, `spec.suspended`, `spec.image`, future toggles | `anki-lifecycle` |
| **activator** | **only** `spec.replicas` (0↔1) | `anki-activator` |
| **operator** | **only** `status.*` (+ the children below) | `anki-operator` |

The operator computes the StatefulSet's replicas as
`effectiveReplicas = suspended ? 0 : replicas` and **never writes any CR spec field**.
Children are reconciled with **Server-Side Apply** under the field manager
`anki-operator`, so the operator never clobbers the activator's `spec.replicas` or the
lifecycle service's spec writes.

## Layout

```
api/v1alpha1/        CRD types + CEL validation + deepcopy
internal/config/     operator config surface (flags/defaults)
internal/controller/ reconciler, StatefulSet/Service builders, rollout, status, metrics
cmd/                 manager entrypoint (leader election, metrics, probes)
config/              kustomize base (crd / rbac / manager / default) + samples
argocd/              packaging notes for the app-of-apps (CRDs early, SSA)
```

## Develop

```sh
make generate      # deepcopy
make manifests     # CRD + RBAC
make build         # go build
make test          # unit + envtest (fetches control-plane binaries)
make test-unit     # pure-function tests only (no apiserver)
make run IMG=repo/anki:tag   # run against the current kubecontext
```

## Config (flags; sane defaults — requirements-operator §6)

| Flag | Default | Meaning |
|---|---|---|
| `--namespace` | `anki-instances` | where CRs + children live |
| `--fleet-image` | *(required)* | pod image the fleet converges to unless `spec.image` overrides |
| `--storage-class` | `hcloud-volumes` | per-user PVC storage class |
| `--pvc-size` | `10Gi` | per-user PVC request |
| `--node-label-key` / `--node-label-value` | `ankimcp.ai/storage` / `hcloud-volumes` | node-affinity target |
| `--rollout-only-when-suspended` | `true` | only re-template a STS at 0 replicas |
| `--rollout-max-concurrent` | `20` | max STSes out-of-date-and-rolling at once |
| `--rollout-paused` | `false` | freeze all image rollout |
| `--run-as-user` / `--fs-group` | `10001` / `10001` | pod uid/gid (= the image's pinned `anki` uid) |
| `--leader-elect` | `false` | enable single-active-reconciler leader election |

## Fleet image rollout (requirements-operator §7)

Suspend-aware and budgeted: a per-CR `spec.image` override wins; suspended instances
are re-templated immediately (nothing rolls — the next wake brings the new image);
awake instances are deferred (the activator idles every user within ~1 TTL, so the
fleet drains onto the new image within roughly one TTL window); a global
`--rollout-max-concurrent` budget bounds simultaneous rollouts; `--rollout-paused`
freezes everything. Progress is exposed as metrics
(`ankiinstance_fleet_instances_total`, `..._on_target_image_total`, `..._rolling_total`)
and mirrored per-CR in `status.currentImage`.

## Deploy

```sh
kubectl apply --server-side -k config/default
```

In argocd, split the CRD into its own early-tier app and set `ServerSideApply=true`;
see [`argocd/README.md`](argocd/README.md). Namespace, PSA label, CiliumNetworkPolicy,
StorageClass/CSI and node labels are **infra-owned** (separate apps).
