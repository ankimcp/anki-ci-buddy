package controller

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

func instance(name string, replicas *int32, suspended bool, image string) *ankiv1alpha1.AnkiInstance {
	return &ankiv1alpha1.AnkiInstance{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "anki-instances"},
		Spec: ankiv1alpha1.AnkiInstanceSpec{
			User:      name,
			Replicas:  replicas,
			Suspended: suspended,
			Image:     image,
		},
	}
}

func stsWithImage(image string) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		Spec: appsv1.StatefulSetSpec{
			Template: corev1.PodTemplateSpec{
				Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "anki", Image: image}}},
			},
		},
	}
}

func TestEffectiveReplicas(t *testing.T) {
	tests := []struct {
		name      string
		replicas  *int32
		suspended bool
		want      int32
	}{
		{"nil replicas -> 0", nil, false, 0},
		{"0 not suspended", ptr.To(int32(0)), false, 0},
		{"1 not suspended", ptr.To(int32(1)), false, 1},
		{"1 suspended -> 0", ptr.To(int32(1)), true, 0},
		{"0 suspended -> 0", ptr.To(int32(0)), true, 0},
		{"suspend gates a nil-but-awake never happens, still 0", nil, true, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := instance("alice", tc.replicas, tc.suspended, "").EffectiveReplicas()
			if got != tc.want {
				t.Fatalf("EffectiveReplicas() = %d, want %d", got, tc.want)
			}
		})
	}
}

func TestTargetImage(t *testing.T) {
	cfg := &config.Config{FleetImage: "repo/anki:fleet"}
	if got := targetImage(instance("a", nil, false, ""), cfg); got != "repo/anki:fleet" {
		t.Fatalf("empty override should use fleet image, got %q", got)
	}
	if got := targetImage(instance("a", nil, false, "repo/anki:canary"), cfg); got != "repo/anki:canary" {
		t.Fatalf("spec.image override must win, got %q", got)
	}
}

func TestStsRolling(t *testing.T) {
	rolling := &appsv1.StatefulSet{Status: appsv1.StatefulSetStatus{CurrentRevision: "a", UpdateRevision: "b"}}
	stable := &appsv1.StatefulSet{Status: appsv1.StatefulSetStatus{CurrentRevision: "a", UpdateRevision: "a"}}
	fresh := &appsv1.StatefulSet{}
	if !stsRolling(rolling) {
		t.Error("expected rolling")
	}
	if stsRolling(stable) {
		t.Error("expected stable")
	}
	if stsRolling(fresh) {
		t.Error("fresh STS (empty revisions) must not count as rolling")
	}
	if stsRolling(nil) {
		t.Error("nil STS must not count as rolling")
	}
}

func TestDecideImage(t *testing.T) {
	fleet := "repo/anki:new"
	old := "repo/anki:old"

	type tc struct {
		name        string
		cfg         *config.Config
		inst        *ankiv1alpha1.AnkiInstance
		sts         *appsv1.StatefulSet
		budget      int
		wantImage   string
		wantRequeue bool
	}
	base := func() *config.Config {
		return &config.Config{FleetImage: fleet, RolloutOnlyWhenSuspended: true, RolloutMaxConcurrent: 20}
	}
	cases := []tc{
		{
			name:      "new instance provisions at target regardless of budget",
			cfg:       base(),
			inst:      instance("a", ptr.To(int32(0)), false, ""),
			sts:       nil,
			budget:    0,
			wantImage: fleet,
		},
		{
			name:      "already on target: no-op",
			cfg:       base(),
			inst:      instance("a", ptr.To(int32(1)), false, ""),
			sts:       stsWithImage(fleet),
			budget:    20,
			wantImage: fleet,
		},
		{
			name:        "awake + onlyWhenSuspended: defer (keep old, requeue)",
			cfg:         base(),
			inst:        instance("a", ptr.To(int32(1)), false, ""),
			sts:         stsWithImage(old),
			budget:      20,
			wantImage:   old,
			wantRequeue: true,
		},
		{
			name:      "suspended: re-template now within budget",
			cfg:       base(),
			inst:      instance("a", ptr.To(int32(1)), true, ""), // suspended -> eff 0
			sts:       stsWithImage(old),
			budget:    20,
			wantImage: fleet,
		},
		{
			name:        "suspended but over budget: defer",
			cfg:         base(),
			inst:        instance("a", ptr.To(int32(0)), false, ""),
			sts:         stsWithImage(old),
			budget:      0,
			wantImage:   old,
			wantRequeue: true,
		},
		{
			name: "paused freezes everything",
			cfg: func() *config.Config {
				c := base()
				c.RolloutPaused = true
				return c
			}(),
			inst:      instance("a", ptr.To(int32(0)), false, ""),
			sts:       stsWithImage(old),
			budget:    20,
			wantImage: old,
		},
		{
			name: "onlyWhenSuspended=false rolls awake pods too",
			cfg: func() *config.Config {
				c := base()
				c.RolloutOnlyWhenSuspended = false
				return c
			}(),
			inst:      instance("a", ptr.To(int32(1)), false, ""),
			sts:       stsWithImage(old),
			budget:    20,
			wantImage: fleet,
		},
		{
			name:      "spec.image override beats fleet image",
			cfg:       base(),
			inst:      instance("a", ptr.To(int32(0)), false, "repo/anki:pin"),
			sts:       stsWithImage(old),
			budget:    20,
			wantImage: "repo/anki:pin",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := decideImage(c.inst, c.cfg, c.sts, c.budget)
			if got.Image != c.wantImage {
				t.Errorf("image = %q, want %q", got.Image, c.wantImage)
			}
			if got.Requeue != c.wantRequeue {
				t.Errorf("requeue = %v, want %v", got.Requeue, c.wantRequeue)
			}
		})
	}
}

func TestComputeStatusPhase(t *testing.T) {
	awake := instance("a", ptr.To(int32(1)), false, "")
	suspended := instance("a", ptr.To(int32(0)), false, "")

	if phase, _ := computeStatus(awake, nil, "img"); phase != ankiv1alpha1.PhaseProvisioning {
		t.Errorf("nil STS should be Provisioning, got %s", phase)
	}

	notReady := &appsv1.StatefulSet{Status: appsv1.StatefulSetStatus{ReadyReplicas: 0}}
	if phase, _ := computeStatus(awake, notReady, "img"); phase != ankiv1alpha1.PhaseStarting {
		t.Errorf("awake + not ready should be Starting, got %s", phase)
	}

	ready := &appsv1.StatefulSet{Status: appsv1.StatefulSetStatus{ReadyReplicas: 1}}
	if phase, r := computeStatus(awake, ready, "img"); phase != ankiv1alpha1.PhaseRunning || r != 1 {
		t.Errorf("awake + ready should be Running/1, got %s/%d", phase, r)
	}

	if phase, _ := computeStatus(suspended, ready, "img"); phase != ankiv1alpha1.PhaseSuspended {
		t.Errorf("eff 0 should be Suspended, got %s", phase)
	}
}
