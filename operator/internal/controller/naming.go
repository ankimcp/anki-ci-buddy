package controller

import (
	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// childName returns the deterministic child object name for a CR: anki-<keycloakId>
// (docs/contracts.md §3). The StatefulSet, headless Service and mounted Secret all
// share this name.
func childName(instance *ankiv1alpha1.AnkiInstance) string {
	return config.ChildNamePrefix + instance.Name
}

// b2SecretName returns the name of the per-user B2/rclone env Secret:
// anki-<keycloakId>-b2 (docs/contracts.md §8/§9). It is a SEPARATE object from the
// credentials Secret (childName) so the hkey NEVER lands in the privileged sidecar's
// env: only this -b2 Secret is envFrom'd into the sidecar; the credentials Secret is
// file-mounted into the anki container ONLY. Written by the lifecycle service.
func b2SecretName(instance *ankiv1alpha1.AnkiInstance) string {
	return childName(instance) + config.SecretSuffixB2
}

// pvcName returns the name of the per-user PVC the StatefulSet controller creates
// from the volumeClaimTemplate: <template>-<stsName>-<ordinal>, i.e.
// profile-anki-<keycloakId>-0 (a per-user unit is a single pod, ordinal 0). The
// operator never creates this PVC; it deletes it in exactly one case — the deletion
// finalizer with spec.dataRetention: Delete (requirements-operator §4.6/§5.4).
func pvcName(instance *ankiv1alpha1.AnkiInstance) string {
	return config.ProfileVolume + "-" + childName(instance) + "-0"
}

// commonLabels are applied to every child and used as the pod selector and the
// network-policy endpointSelector (docs/contracts.md §5).
func commonLabels(instance *ankiv1alpha1.AnkiInstance) map[string]string {
	return map[string]string{
		config.LabelName:      config.LabelNameValue,
		config.LabelManagedBy: config.LabelManagedByValue,
		config.LabelInstance:  childName(instance),
		config.LabelUser:      instance.Name,
	}
}
