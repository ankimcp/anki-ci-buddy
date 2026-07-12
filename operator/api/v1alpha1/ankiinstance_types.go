/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License").
*/

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// AnkiInstanceSpec defines the desired state of an AnkiInstance.
//
// Field ownership is a hard control-plane contract (docs/contracts.md §2):
//   - spec.user, spec.suspended, spec.image  -> lifecycle service (anki-lifecycle)
//   - spec.replicas                          -> activator          (anki-activator)
//   - status.*                               -> operator           (anki-operator)
//
// The operator never writes any spec field. It computes the child StatefulSet's
// replicas as effectiveReplicas = (suspended ? 0 : replicas).
type AnkiInstanceSpec struct {
	// User is the tenant identity (the Keycloak sub / keycloakId). It is the same
	// value as metadata.name and is immutable: it seeds the PVC / Secret / B2-prefix
	// identity, so changing it would strand a disk (docs/contracts.md §3).
	//
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="spec.user is immutable"
	User string `json:"user"`

	// Replicas is the wake/idle signal, constrained to {0,1}: a per-user unit is
	// a single pod (RWOP structurally forbids two pods on the disk). 0 =
	// idle-suspended, 1 = awake. LIFECYCLE-OWNED under the v1 single-writer
	// contract (docs/contracts.md §2; activator shelved 2026-07-11); the
	// operator reads it but never writes it.
	//
	// +kubebuilder:default=0
	// +kubebuilder:validation:XValidation:rule="self in [0, 1]",message="spec.replicas must be 0 or 1"
	// +optional
	Replicas *int32 `json:"replicas,omitempty"`

	// Suspended is the lifecycle-owned administrative power gate (dashboard on/off),
	// orthogonal to the activator's replicas. When true the operator forces the
	// StatefulSet to 0 regardless of replicas. LIFECYCLE-OWNED (docs/contracts.md §2).
	//
	// +kubebuilder:default=false
	// +optional
	Suspended bool `json:"suspended,omitempty"`

	// RestartedAt is an optional RFC3339 timestamp acting as a restart nonce
	// (requirements-operator §3.1, added 2026-07-11). When set/changed the operator
	// copies the value VERBATIM into the StatefulSet pod template as the
	// anki.ankimcp.ai/restartedAt annotation (the `kubectl rollout restart`
	// pattern): the template hash changes, the StatefulSet controller recreates the
	// pod, and the PVC is untouched. Empty/absent = no annotation. This is the
	// platform's only restart mechanism (no live pod surgery). LIFECYCLE-OWNED.
	//
	// +optional
	RestartedAt string `json:"restartedAt,omitempty"`

	// DataRetention governs the per-user PVC's fate when the CR is deleted
	// (requirements-operator §3.1/§5.4, added 2026-07-11): with Delete the
	// operator's deletion finalizer deletes the PVC before releasing; with Retain
	// (default, fail-safe) the PVC survives. This keeps the lifecycle service's
	// RBAC free of workload-object verbs (docs/contracts.md §2). LIFECYCLE-OWNED.
	//
	// +kubebuilder:validation:Enum=Retain;Delete
	// +kubebuilder:default=Retain
	// +optional
	DataRetention DataRetentionPolicy `json:"dataRetention,omitempty"`

	// Image is an optional per-instance image PIN / override (canary a single user).
	// Empty means use the operator's fleet image (requirements-operator §7).
	// LIFECYCLE-OWNED.
	//
	// +optional
	Image string `json:"image,omitempty"`

	// AnkiConnect is a reserved future toggle to gate the AnkiConnect surface.
	// Present in the schema for forward-compat; it is inert in v1 (no-op).
	//
	// TODO(open-decision): AnkiConnect is deferred until demand (ARCHITECTURE §9,
	// decision 12). Wire it to a container port / config only when the image and
	// activator support it.
	//
	// +kubebuilder:default=false
	// +optional
	AnkiConnect bool `json:"ankiConnect,omitempty"`
}

// DataRetentionPolicy is the PVC fate on CR delete (spec.dataRetention).
type DataRetentionPolicy string

const (
	// DataRetentionRetain (default): the per-user PVC survives CR deletion.
	DataRetentionRetain DataRetentionPolicy = "Retain"
	// DataRetentionDelete: the operator's deletion finalizer deletes the per-user
	// PVC before releasing (requirements-operator §5.4).
	DataRetentionDelete DataRetentionPolicy = "Delete"
)

// AnkiInstancePhase is a coarse, human-facing summary of an instance's state.
// Conditions are the machine-facing truth (requirements-operator §3.2).
// +kubebuilder:validation:Enum=Provisioning;Suspended;Starting;Running;Error
type AnkiInstancePhase string

const (
	// PhaseProvisioning : CR seen, children not yet all created / never yet ready.
	PhaseProvisioning AnkiInstancePhase = "Provisioning"
	// PhaseSuspended : effective desired replicas 0 (idle or admin-off); a healthy
	// resting state, not an error. PVC retained.
	PhaseSuspended AnkiInstancePhase = "Suspended"
	// PhaseStarting : effective desired replicas 1, pod not yet Ready (wake in progress).
	PhaseStarting AnkiInstancePhase = "Starting"
	// PhaseRunning : pod Ready.
	PhaseRunning AnkiInstancePhase = "Running"
	// PhaseError : a child failed to apply, or a wedged state the operator surfaces
	// but cannot itself resolve.
	PhaseError AnkiInstancePhase = "Error"
)

// Condition types set by the operator.
const (
	// ConditionReady is True when the instance is in the state its spec asks for
	// (running, or correctly suspended). Not "pod serving" — a suspended instance
	// is correctly off, not unready (requirements-operator §3.2).
	//
	// TODO(open-decision): §12 item 8 — confirm "Ready = in-desired-state" reads
	// well for dashboard/activator consumers, or invert to "Ready = pod serving".
	ConditionReady = "Ready"
	// ConditionProgressing is True while a child change is rolling out.
	ConditionProgressing = "Progressing"
	// ConditionDegraded latches a reconcile/child error.
	ConditionDegraded = "Degraded"
)

// AnkiInstanceStatus defines the observed state of an AnkiInstance. Written only
// by the operator, via the status subresource (never bumps metadata.generation).
type AnkiInstanceStatus struct {
	// Phase is a coarse human-facing summary (see AnkiInstancePhase).
	// +optional
	Phase AnkiInstancePhase `json:"phase,omitempty"`

	// ObservedGeneration is the .metadata.generation last reconciled, so callers
	// (lifecycle service, activator) can tell whether the operator has caught up.
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Replicas mirrors the StatefulSet's ready replicas (0/1) for kubectl/printers.
	// +optional
	Replicas int32 `json:"replicas"`

	// CurrentImage is the image the live/last-applied pod template carries, for
	// eyeballing a fleet rollout.
	// +optional
	CurrentImage string `json:"currentImage,omitempty"`

	// Conditions follow the standard metav1.Condition shape (Ready/Progressing/Degraded).
	// +optional
	// +patchMergeKey=type
	// +patchStrategy=merge
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// AnkiInstance is the Schema for the ankiinstances API. One per user; the CR
// metadata.name is the keycloakId, and children are named anki-<keycloakId>.
//
// The root CEL rule enforces metadata.name == spec.user (both the keycloakId);
// metadata.name is one of the two metadata fields CEL can read at the object root.
//
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:path=ankiinstances,singular=ankiinstance,shortName=anki,scope=Namespaced,categories=ankimcp
// +kubebuilder:validation:XValidation:rule="self.metadata.name == self.spec.user",message="metadata.name must equal spec.user (both are the keycloakId)"
// +kubebuilder:printcolumn:name="User",type=string,JSONPath=`.spec.user`
// +kubebuilder:printcolumn:name="Desired",type=integer,JSONPath=`.spec.replicas`,description="activator intent"
// +kubebuilder:printcolumn:name="Suspended",type=boolean,JSONPath=`.spec.suspended`,description="admin gate"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=`.status.replicas`,description="ready pods (0/1)"
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=`.status.currentImage`,priority=1
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type AnkiInstance struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	// +optional
	Spec AnkiInstanceSpec `json:"spec,omitempty"`
	// +optional
	Status AnkiInstanceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AnkiInstanceList contains a list of AnkiInstance.
type AnkiInstanceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AnkiInstance `json:"items"`
}

// EffectiveReplicas is the operator's derived desired StatefulSet replicas:
//
//	effectiveReplicas = (spec.suspended ? 0 : spec.replicas)
//
// This is a pure function of spec and is the single place the rule lives
// (docs/contracts.md §2, requirements-operator §4.3).
func (a *AnkiInstance) EffectiveReplicas() int32 {
	if a.Spec.Suspended {
		return 0
	}
	if a.Spec.Replicas == nil {
		return 0
	}
	if *a.Spec.Replicas < 0 {
		return 0
	}
	if *a.Spec.Replicas > 1 {
		return 1
	}
	return *a.Spec.Replicas
}

func init() {
	SchemeBuilder.Register(&AnkiInstance{}, &AnkiInstanceList{})
}
