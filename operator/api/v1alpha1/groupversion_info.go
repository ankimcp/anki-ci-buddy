// Package v1alpha1 contains API Schema definitions for the anki v1alpha1 API group.
//
// The shared cross-component contract (docs/contracts.md §1) fixes the GVR as
// group "anki.ankimcp.ai", version "v1alpha1", kind "AnkiInstance". It must not
// diverge from that file.
//
// +kubebuilder:object:generate=true
// +groupName=anki.ankimcp.ai
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// GroupVersion is the group/version used to register these objects.
	GroupVersion = schema.GroupVersion{Group: "anki.ankimcp.ai", Version: "v1alpha1"}

	// SchemeBuilder is used to add go types to the GroupVersionKind scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the types in this group-version to the given scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
