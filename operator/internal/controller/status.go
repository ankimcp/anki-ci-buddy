package controller

import (
	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
)

// computeStatus derives the desired status from the CR spec and the live child
// StatefulSet (requirements-operator §3.2). sts may be nil (not yet created).
// currentImage is the image the operator applied this reconcile. It returns a
// fully-populated AnkiInstanceStatus (conditions computed via setConditions on the
// live status to preserve lastTransitionTime).
func computeStatus(instance *ankiv1alpha1.AnkiInstance, sts *appsv1.StatefulSet, currentImage string) (ankiv1alpha1.AnkiInstancePhase, int32) {
	eff := instance.EffectiveReplicas()

	if sts == nil {
		// Children not yet created.
		return ankiv1alpha1.PhaseProvisioning, 0
	}

	ready := sts.Status.ReadyReplicas

	switch {
	case eff == 0:
		// Healthy resting state (idle or admin-off).
		return ankiv1alpha1.PhaseSuspended, ready
	case ready >= 1:
		return ankiv1alpha1.PhaseRunning, ready
	default:
		// Desired awake but pod not yet Ready.
		return ankiv1alpha1.PhaseStarting, ready
	}
}

// setConditions writes the Ready/Progressing/Degraded conditions onto status using
// meta.SetStatusCondition semantics (preserving lastTransitionTime on no-change).
func setConditions(status *ankiv1alpha1.AnkiInstanceStatus, phase ankiv1alpha1.AnkiInstancePhase, generation int64, rolling bool, applyErr error) {
	// Ready: True when the instance is in the state its spec asks for.
	ready := metav1.Condition{
		Type:               ankiv1alpha1.ConditionReady,
		ObservedGeneration: generation,
	}
	switch phase {
	case ankiv1alpha1.PhaseRunning:
		ready.Status = metav1.ConditionTrue
		ready.Reason = "PodReady"
		ready.Message = "Pod is Running and Ready"
	case ankiv1alpha1.PhaseSuspended:
		ready.Status = metav1.ConditionTrue
		ready.Reason = "Suspended"
		ready.Message = "Instance is suspended as intended (effective replicas 0)"
	case ankiv1alpha1.PhaseError:
		ready.Status = metav1.ConditionFalse
		ready.Reason = "Error"
		ready.Message = "Instance is in an error state"
	default:
		ready.Status = metav1.ConditionFalse
		ready.Reason = string(phase)
		ready.Message = "Instance is not yet in its desired state"
	}
	meta.SetStatusCondition(&status.Conditions, ready)

	// Progressing: a child change is rolling out (or the pod is starting).
	progressing := metav1.Condition{
		Type:               ankiv1alpha1.ConditionProgressing,
		ObservedGeneration: generation,
	}
	if rolling || phase == ankiv1alpha1.PhaseStarting || phase == ankiv1alpha1.PhaseProvisioning {
		progressing.Status = metav1.ConditionTrue
		progressing.Reason = "Progressing"
		progressing.Message = "A child change is rolling out"
	} else {
		progressing.Status = metav1.ConditionFalse
		progressing.Reason = "Stable"
		progressing.Message = "No child change in progress"
	}
	meta.SetStatusCondition(&status.Conditions, progressing)

	// Degraded: latch a reconcile/child error.
	degraded := metav1.Condition{
		Type:               ankiv1alpha1.ConditionDegraded,
		ObservedGeneration: generation,
	}
	if applyErr != nil {
		degraded.Status = metav1.ConditionTrue
		degraded.Reason = "ReconcileError"
		degraded.Message = applyErr.Error()
	} else {
		degraded.Status = metav1.ConditionFalse
		degraded.Reason = "None"
		degraded.Message = "No errors"
	}
	meta.SetStatusCondition(&status.Conditions, degraded)
}
