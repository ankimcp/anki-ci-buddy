# argocd packaging notes (do NOT wire into the real infra repo here)

This operator is deployed as an argocd app under the app-of-apps pattern
(`argocd/apps/anki-operator/` in the infrastructure repo — ARCHITECTURE §13,
requirements-operator §9). This directory only documents the contract the infra
repo must honour; the actual Application manifests live there.

## Tiering (CRDs first)

Ship **two** apps, not one:

1. **`anki-operator-crd`** — an *early tier* app containing only
   `config/crd/bases/` (the CRD). It must land before the operator and before any
   lifecycle-service / activator app that creates `AnkiInstance` objects.
2. **`anki-operator`** — a *mid tier* app containing `config/rbac` + `config/manager`
   (RBAC, ServiceAccounts, Deployment).

Do **not** bundle the CRD behind the operator Deployment — it must be its own
manifest set so it applies first.

## Application settings

```yaml
# argocd Application (illustrative — the real one lives in the infra repo)
spec:
  syncPolicy:
    syncOptions:
      - ServerSideApply=true   # CRDs are large (full schema); SSA field managers let
                               # argocd and the operator co-own objects without thrash
                               # (requirements-operator §5.3, ARCHITECTURE §12 item 11)
```

- **`ServerSideApply=true` is required.** The operator writes children under the
  field manager `anki-operator`; argocd writes the operator's own manifests under
  its manager. Distinct managers mean no fighting.
- **No sync-wave ordering is assumed.** Sync waves are currently non-functional on
  this cluster (all apps apply at once, converge by retry). The operator tolerates
  racing dependencies: it retries cleanly if its CRD is not registered yet, and its
  reconcile is level-triggered and idempotent, so a child that cannot be created yet
  simply retries.

## Infra-owned prerequisites (separate apps — the operator declares no ordering on them)

- The `anki-instances` **Namespace** + its PSA **`privileged`** label. The default
  `--media-mount-mode=sidecar` renders a privileged per-user rclone sidecar
  (Bidirectional mount propagation requires `privileged: true`; ARCHITECTURE §10
  layer 3, docs/contracts.md §4). The namespace holds ONLY Anki pods and is walled
  in by the default-deny CiliumNetworkPolicy. (If deploying with
  `--media-mount-mode=external`/CSI instead, the pod stays PSA-`baseline`.)
- The namespace-wide **`CiliumNetworkPolicy`** (default-deny + the shared allow-list).
  The operator holds NO Cilium RBAC; it only labels pods so the policy's
  `endpointSelector` selects them.
- The **`hcloud-csi` StorageClass** `hcloud-volumes`.
- The **`ankimcp.ai/storage: hcloud-volumes` node label** on Cloud-Volume nodes
  (does not exist today — infra must create it).
- Per-user **B2 keys** via the existing Bitwarden -> Vault -> ESO pipeline, written
  into each user's `anki-<keycloakId>` Secret by the lifecycle service.

Set the operator's `--fleet-image` (in `config/manager/manager.yaml`) to the current
headless-anki pod image before deploying.
