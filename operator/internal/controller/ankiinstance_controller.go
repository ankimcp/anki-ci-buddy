/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License").
*/

package controller

import (
	"context"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// AnkiInstanceReconciler reconciles an AnkiInstance CR into its per-user children
// (StatefulSet + headless Service + Secret mount) and keeps them converged.
//
// The operator is a dumb, self-healing reconciler: it translates declared CR intent
// into children and writes only status. It NEVER writes any CR spec field
// (docs/contracts.md §2) — in particular it never touches spec.replicas (the
// lifecycle service's signal under the v1 single-writer contract). Children are
// reconciled with Server-Side Apply
// under the field manager "anki-operator" so they never fight other writers.
type AnkiInstanceReconciler struct {
	client.Client
	Scheme   *runtime.Scheme
	Cfg      *config.Config
	Recorder record.EventRecorder
	// APIReader is an UNCACHED reader (mgr.GetAPIReader()) for the few reads that
	// must not be served from a lagging informer cache: the restartedAt
	// carry-forward source (restartedAtValue, statefulset.go) and the finalizer's
	// "is the StatefulSet really gone" check (reconcileDelete). Defaulted in
	// SetupWithManager when nil.
	APIReader client.Reader
}

// RBAC — least privilege (requirements-operator §8). Note the deliberate ABSENCES:
// no secrets verbs (the operator must never read user hkeys; mounting a Secret needs
// no RBAC) and no ciliumnetworkpolicies (the namespace-wide policy is infra-owned).
//
// The ankiinstances/finalizers grant serves the deletion finalizer (§5.4, required
// since 2026-07-11). PVC `delete` exists for the data-fate finalizer flow ONLY
// (spec.dataRetention: Delete); the operator still never CREATES PVCs (§4.6).
//+kubebuilder:rbac:groups=anki.ankimcp.ai,resources=ankiinstances,verbs=get;list;watch;update;patch
//+kubebuilder:rbac:groups=anki.ankimcp.ai,resources=ankiinstances/status,verbs=get;update;patch
//+kubebuilder:rbac:groups=anki.ankimcp.ai,resources=ankiinstances/finalizers,verbs=update
//+kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups="",resources=persistentvolumeclaims,verbs=get;list;watch;delete
//+kubebuilder:rbac:groups="",resources=events,verbs=create;patch

// Reconcile is level-triggered and idempotent (requirements-operator §5.2).
func (r *AnkiInstanceReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	// 1. Fetch the CR. If gone, ownerRef GC removes children — nothing to do (the
	//    finalizer has already run; PVC fate was decided by spec.dataRetention).
	var instance ankiv1alpha1.AnkiInstance
	if err := r.Get(ctx, req.NamespacedName, &instance); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get AnkiInstance: %w", err)
	}

	// 1b. CR being deleted: run the data-fate finalizer (§5.4) and stop. Children
	//     are collected by ownerRef GC; the finalizer only decides the PVC's fate.
	if !instance.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, &instance)
	}

	// 1c. Ensure the data-fate finalizer is present (idempotent — AddFinalizer
	//     reports whether it changed anything). Added unconditionally, not only for
	//     dataRetention: Delete, so a Retain->Delete flip right before deletion
	//     still gets its cleanup; the Retain path just releases without touching
	//     the PVC.
	//
	//     Deliberately a plain full-object Update under the default field manager —
	//     NOT SSA/patch: the operator's SSA field ownership (manager "anki-operator")
	//     stays confined to its apply paths (children + status), so finalizer
	//     bookkeeping never claims CR fields in managedFields (same rationale at the
	//     RemoveFinalizer in reconcileDelete).
	if controllerutil.AddFinalizer(&instance, config.Finalizer) {
		if err := r.Update(ctx, &instance); err != nil {
			return ctrl.Result{}, fmt.Errorf("add finalizer: %w", err)
		}
	}

	// 2. Read the existing StatefulSet (may not exist yet) so we can make rollout
	//    and status decisions off live child state.
	var stsPtr *appsv1.StatefulSet
	var existing appsv1.StatefulSet
	getErr := r.Get(ctx, client.ObjectKey{Namespace: instance.Namespace, Name: childName(&instance)}, &existing)
	switch {
	case getErr == nil:
		stsPtr = &existing
	case apierrors.IsNotFound(getErr):
		stsPtr = nil
	default:
		return ctrl.Result{}, fmt.Errorf("get StatefulSet: %w", getErr)
	}

	// 3. Decide the pod image via the suspend-aware, budgeted rollout policy (§7).
	eff := instance.EffectiveReplicas()
	budgetAvailable := 0
	if r.needsBudget(&instance, stsPtr, eff) {
		inFlight, err := r.countInFlightRollouts(ctx)
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("count in-flight rollouts: %w", err)
		}
		budgetAvailable = r.Cfg.RolloutMaxConcurrent - inFlight
	}
	decision := decideImage(&instance, r.Cfg, stsPtr, budgetAvailable)

	// Guard: an instance with no resolved image (no spec.image and no fleetImage)
	// cannot produce a valid pod. Surface it as Error rather than apply garbage.
	if decision.Image == "" {
		return r.failWithStatus(ctx, &instance, stsPtr,
			fmt.Errorf("no image configured: set spec.image or the operator --fleet-image"))
	}

	// 4. Apply children with Server-Side Apply (§5.3). Service first, then STS.
	svc := desiredService(&instance, r.Cfg)
	if err := r.applyChild(ctx, &instance, svc); err != nil {
		return r.failWithStatus(ctx, &instance, stsPtr, fmt.Errorf("apply Service: %w", err))
	}

	restartedAt, err := r.restartedAtValue(ctx, &instance, stsPtr)
	if err != nil {
		return r.failWithStatus(ctx, &instance, stsPtr, fmt.Errorf("resolve restartedAt carry-forward: %w", err))
	}
	sts := desiredStatefulSet(&instance, r.Cfg, decision.Image, eff, restartedAt)
	if err := r.applyChild(ctx, &instance, sts); err != nil {
		return r.failWithStatus(ctx, &instance, stsPtr, fmt.Errorf("apply StatefulSet: %w", err))
	}

	// 5. Write status from the (pre-apply) child state + the image we just applied.
	if err := r.writeStatus(ctx, &instance, stsPtr, decision.Image, nil); err != nil {
		return ctrl.Result{}, err
	}

	// 6. Requeue only if a rollout is deferred (awake under onlyWhenSuspended, or
	//    over budget). Steady state is watch-driven.
	if decision.Requeue {
		logger.V(1).Info("rollout deferred; requeueing", "instance", instance.Name,
			"current", stsImage(stsPtr), "target", targetImage(&instance, r.Cfg))
		return ctrl.Result{RequeueAfter: 2 * time.Minute}, nil
	}
	return ctrl.Result{}, nil
}

// reconcileDelete runs the deletion finalizer (requirements-operator §5.4): with
// spec.dataRetention: Delete it deletes the child StatefulSet FIRST, requeues until
// the STS is actually gone, then deletes the per-user PVC — the spec's "after the
// STS/pod is gone" — and only then releases the finalizer; with Retain (default) it
// releases without touching anything (children are collected by ownerRef GC). The
// STS-first ordering is load-bearing: deleting the PVC while the STS still exists
// leaves a window where an out-of-band pod death makes the STS controller recreate
// pod+PVC — an orphaned fresh volume. Idempotent and crash-safe: every delete
// tolerates NotFound and a crash between steps simply re-runs them. B2
// deprovisioning stays a lifecycle-service action — never the operator's.
func (r *AnkiInstanceReconciler) reconcileDelete(ctx context.Context, instance *ankiv1alpha1.AnkiInstance) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(instance, config.Finalizer) {
		// Nothing to do: the finalizer already ran (or was never added).
		return ctrl.Result{}, nil
	}

	if instance.Spec.DataRetention == ankiv1alpha1.DataRetentionDelete {
		// 1. Delete the child StatefulSet explicitly (NotFound-tolerant: ownerRef GC
		//    may already have collected it).
		stsKey := client.ObjectKey{Namespace: instance.Namespace, Name: childName(instance)}
		sts := &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Namespace: stsKey.Namespace, Name: stsKey.Name}}
		if err := r.Delete(ctx, sts); err != nil && !apierrors.IsNotFound(err) {
			return ctrl.Result{}, fmt.Errorf("delete StatefulSet %s (dataRetention: Delete): %w", stsKey.Name, err)
		}

		// 2. Confirm the STS is ACTUALLY gone before touching the PVC. Uncached read
		//    (APIReader): the informer cache lags the delete, so a cached Get could
		//    report "gone" while the STS still exists. If it lingers (e.g. foreground
		//    deletion, its own finalizers), requeue and re-check — the PVC must not
		//    be deleted while the STS could still recreate it.
		var live appsv1.StatefulSet
		switch err := r.APIReader.Get(ctx, stsKey, &live); {
		case err == nil:
			return ctrl.Result{RequeueAfter: time.Second}, nil
		case !apierrors.IsNotFound(err):
			return ctrl.Result{}, fmt.Errorf("confirm StatefulSet %s deletion: %w", stsKey.Name, err)
		}

		// 3. STS gone: now delete the PVC (NotFound-tolerant — an instance that
		//    never woke has no PVC).
		pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{
			Namespace: instance.Namespace,
			Name:      pvcName(instance),
		}}
		if err := r.Delete(ctx, pvc); err != nil && !apierrors.IsNotFound(err) {
			// Keep the finalizer: the CR stays terminating and we retry with backoff.
			return ctrl.Result{}, fmt.Errorf("delete PVC %s (dataRetention: Delete): %w", pvc.Name, err)
		}
		if r.Recorder != nil {
			r.Recorder.Event(instance, corev1.EventTypeNormal, "PVCDeleted",
				fmt.Sprintf("Deleted PVC %s (spec.dataRetention: Delete)", pvc.Name))
		}
	}

	// Plain full-object Update under the default field manager — NOT SSA/patch — so
	// the operator's SSA ownership stays confined to its apply paths (see the
	// AddFinalizer comment in Reconcile).
	controllerutil.RemoveFinalizer(instance, config.Finalizer)
	if err := r.Update(ctx, instance); err != nil {
		return ctrl.Result{}, fmt.Errorf("remove finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

// needsBudget reports whether this reconcile might advance an existing STS onto a
// new image (the only branch that consumes the global concurrency budget). Avoids
// a fleet-wide list on every reconcile.
func (r *AnkiInstanceReconciler) needsBudget(instance *ankiv1alpha1.AnkiInstance, sts *appsv1.StatefulSet, eff int32) bool {
	if sts == nil {
		return false // new instance: provisioned at target regardless of budget
	}
	if stsImage(sts) == targetImage(instance, r.Cfg) {
		return false // already on target
	}
	if r.Cfg.RolloutPaused {
		return false // frozen; keeps current image
	}
	if r.Cfg.RolloutOnlyWhenSuspended && eff != 0 {
		return false // deferred; won't advance this pass
	}
	return true
}

// countInFlightRollouts counts managed StatefulSets mid-transition between pod
// revisions (requirements-operator §7 step 4). Leader-scoped single operator makes
// this a good-enough fleet view; note the mild race under concurrent reconciles.
func (r *AnkiInstanceReconciler) countInFlightRollouts(ctx context.Context) (int, error) {
	var list appsv1.StatefulSetList
	if err := r.List(ctx, &list,
		client.InNamespace(r.Cfg.Namespace),
		client.MatchingLabels{config.LabelManagedBy: config.LabelManagedByValue},
	); err != nil {
		return 0, err
	}
	count := 0
	for i := range list.Items {
		if stsRolling(&list.Items[i]) {
			count++
		}
	}
	return count, nil
}

// applyChild sets the controller ownerRef and Server-Side-Applies the object under
// the operator's field manager, forcing ownership of the fields it manages.
func (r *AnkiInstanceReconciler) applyChild(ctx context.Context, instance *ankiv1alpha1.AnkiInstance, obj client.Object) error {
	if err := controllerutil.SetControllerReference(instance, obj, r.Scheme); err != nil {
		return fmt.Errorf("set controller reference: %w", err)
	}
	return r.Patch(ctx, obj, client.Apply,
		client.FieldOwner(config.FieldOwner),
		client.ForceOwnership,
	)
}

// writeStatus computes and persists the CR status via the status subresource (never
// touching spec). applyErr, when non-nil, latches Degraded and forces Phase=Error.
//
// Status is written with Server-Side Apply under the field manager "anki-operator"
// (docs/contracts.md §2: status is owned by the SSA manager anki-operator, matching
// the manager the operator uses for children). We apply an unstructured object that
// carries ONLY status — never spec — so the operator claims ownership of status
// fields alone and can never fight the activator's spec.replicas or lifecycle's spec
// writes. It is the single writer of status, so ForceOwnership is safe.
func (r *AnkiInstanceReconciler) writeStatus(ctx context.Context, instance *ankiv1alpha1.AnkiInstance, sts *appsv1.StatefulSet, currentImage string, applyErr error) error {
	phase, ready := computeStatus(instance, sts, currentImage)
	if applyErr != nil {
		phase = ankiv1alpha1.PhaseError
	}

	instance.Status.Phase = phase
	instance.Status.Replicas = ready
	instance.Status.CurrentImage = currentImage
	instance.Status.ObservedGeneration = instance.Generation
	setConditions(&instance.Status, phase, instance.Generation, stsRolling(sts), applyErr)

	// Build a status-only apply object. Serializing the typed CR would also emit its
	// (zero-valued) spec — encoding/json cannot omit a non-pointer struct — which
	// would send spec to the status subresource. An unstructured status-only body
	// keeps the write strictly status.*.
	statusMap, err := runtime.DefaultUnstructuredConverter.ToUnstructured(&instance.Status)
	if err != nil {
		return fmt.Errorf("convert status to unstructured: %w", err)
	}
	apply := &unstructured.Unstructured{}
	apply.SetGroupVersionKind(ankiv1alpha1.GroupVersion.WithKind("AnkiInstance"))
	apply.SetName(instance.Name)
	apply.SetNamespace(instance.Namespace)
	if err := unstructured.SetNestedMap(apply.Object, statusMap, "status"); err != nil {
		return fmt.Errorf("set status on apply object: %w", err)
	}

	if err := r.Status().Patch(ctx, apply, client.Apply,
		client.FieldOwner(config.FieldOwner),
		client.ForceOwnership,
	); err != nil {
		if apierrors.IsConflict(err) {
			// Lost a race with another status write; requeue to recompute.
			return fmt.Errorf("status apply conflict: %w", err)
		}
		return fmt.Errorf("apply status: %w", err)
	}
	return nil
}

// failWithStatus records an event, writes an Error status, and returns the error so
// the workqueue backs off.
func (r *AnkiInstanceReconciler) failWithStatus(ctx context.Context, instance *ankiv1alpha1.AnkiInstance, sts *appsv1.StatefulSet, cause error) (ctrl.Result, error) {
	if r.Recorder != nil {
		r.Recorder.Event(instance, corev1.EventTypeWarning, "ReconcileError", cause.Error())
	}
	// Best-effort status write; return the original cause regardless.
	if serr := r.writeStatus(ctx, instance, sts, stsImage(sts), cause); serr != nil {
		log.FromContext(ctx).Error(serr, "failed to write error status")
	}
	return ctrl.Result{}, cause
}

// SetupWithManager wires the controller: reconcile the CR, watch owned children so
// their status changes re-trigger reconcile (requirements-operator §5.1).
func (r *AnkiInstanceReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Recorder == nil {
		r.Recorder = mgr.GetEventRecorderFor("anki-operator")
	}
	if r.APIReader == nil {
		r.APIReader = mgr.GetAPIReader()
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&ankiv1alpha1.AnkiInstance{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.Service{}).
		WithOptions(controller.Options{MaxConcurrentReconciles: r.Cfg.MaxConcurrentReconciles}).
		Named("ankiinstance").
		Complete(r)
}
