package controller

import (
	appsv1 "k8s.io/api/apps/v1"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// targetImage resolves the image an instance should converge to: a per-CR
// spec.image override wins, else the operator's fleet image
// (requirements-operator §7 step 1-2).
func targetImage(instance *ankiv1alpha1.AnkiInstance, cfg *config.Config) string {
	if instance.Spec.Image != "" {
		return instance.Spec.Image
	}
	return cfg.FleetImage
}

// stsImage extracts the "anki" container image from a live StatefulSet, or "" if
// the STS has no such container yet.
func stsImage(sts *appsv1.StatefulSet) string {
	if sts == nil {
		return ""
	}
	for _, c := range sts.Spec.Template.Spec.Containers {
		if c.Name == "anki" {
			return c.Image
		}
	}
	return ""
}

// stsRolling reports whether a StatefulSet is mid-transition between pod-template
// revisions. currentRevision != updateRevision is the clean k8s signal for "a
// rollout is in progress" and is what the budget in §7 step 4 counts.
//
// Budget honesty (docs/requirements-operator §7): this signal — and therefore the
// --rollout-max-concurrent budget built on it (countInFlightRollouts) — bounds only
// instances that are actively *rolling* their pods. A re-templated STS at
// effectiveReplicas=0 (a suspended/idle instance) reconciles its revision instantly
// with no pod churn, so it barely occupies the budget. The real image-pull herd is
// not the re-template; it is the *wake* (0->1), when a fresh pod pulls the new image
// for the first time. That wake-time pull pressure is NOT gated here — it is bounded
// upstream by the activator's rate-limited, single-flight wake (docs/contracts.md §2;
// ARCHITECTURE §4), which serializes 0->1 transitions per user and fleet-wide.
//
// TODO(rollout): consider gating on a desired-vs-applied image transition (not just
// currentRevision!=updateRevision) to *also* bound wake-time pulls from the operator
// side, rather than relying solely on the activator's wake rate limiter.
func stsRolling(sts *appsv1.StatefulSet) bool {
	if sts == nil {
		return false
	}
	cur := sts.Status.CurrentRevision
	upd := sts.Status.UpdateRevision
	// Empty revisions (freshly created / envtest without the STS controller) are
	// not "rolling".
	if cur == "" || upd == "" {
		return false
	}
	return cur != upd
}

// imageDecision is the result of the suspend-aware, budgeted rollout evaluation.
type imageDecision struct {
	// Image is what the operator should put in the STS pod template this reconcile.
	Image string
	// Requeue signals the caller to re-enqueue: the instance wants a newer image
	// but is deferred (awake under onlyWhenSuspended, or over the concurrency budget).
	Requeue bool
}

// decideImage implements the fleet rollout policy (requirements-operator §7):
//
//   - New instance (STS absent): provision at target immediately — rollout policy
//     only governs CHANGING an existing STS, not initial provisioning.
//   - Already at target: nothing to do.
//   - paused: freeze — keep the current image.
//   - onlyWhenSuspended && awake: defer (requeue) rather than restart an active user;
//     the activator idles every user within ~1 TTL, so the fleet drains naturally.
//   - suspended (or onlyWhenSuspended disabled): advance if within the concurrency
//     budget; otherwise defer.
//
// budgetAvailable is the number of additional instances that may start rolling this
// pass (maxConcurrent minus the count already mid-transition across the fleet); it
// is ignored when the instance is suspended-and-not-rolling only insofar as we still
// consume from it to bound wake-time image-pull/B2 pressure.
func decideImage(instance *ankiv1alpha1.AnkiInstance, cfg *config.Config, sts *appsv1.StatefulSet, budgetAvailable int) imageDecision {
	target := targetImage(instance, cfg)

	// New instance: provision at target now.
	if sts == nil {
		return imageDecision{Image: target}
	}

	current := stsImage(sts)

	// Up to date.
	if current == target {
		return imageDecision{Image: current}
	}

	// A change is wanted. If rollout is frozen, hold the current image.
	if cfg.RolloutPaused {
		return imageDecision{Image: current}
	}

	effReplicas := instance.EffectiveReplicas()

	// Suspend-aware: don't restart an awake user mid-session.
	if cfg.RolloutOnlyWhenSuspended && effReplicas != 0 {
		return imageDecision{Image: current, Requeue: true}
	}

	// Eligible to roll: respect the global concurrency budget.
	if budgetAvailable <= 0 {
		return imageDecision{Image: current, Requeue: true}
	}

	return imageDecision{Image: target}
}
