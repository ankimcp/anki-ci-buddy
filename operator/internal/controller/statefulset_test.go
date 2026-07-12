package controller

import (
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// These are pure-function tests over desiredStatefulSet — no apiserver — so they
// can exercise BOTH media-mount modes (the envtest suite runs a single manager with
// the default "sidecar" config; external mode is covered here).

func testInstance() *ankiv1alpha1.AnkiInstance {
	return &ankiv1alpha1.AnkiInstance{
		ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "anki-instances"},
		Spec:       ankiv1alpha1.AnkiInstanceSpec{User: "alice", Replicas: ptr.To(int32(1))},
	}
}

func findVolumeMount(mounts []corev1.VolumeMount, name string) *corev1.VolumeMount {
	for i := range mounts {
		if mounts[i].Name == name {
			return &mounts[i]
		}
	}
	return nil
}

func findEnv(envs []corev1.EnvVar, name string) *corev1.EnvVar {
	for i := range envs {
		if envs[i].Name == name {
			return &envs[i]
		}
	}
	return nil
}

func TestDesiredStatefulSet_SidecarMode(t *testing.T) {
	cfg := config.Default()
	cfg.MediaMountMode = config.MediaMountModeSidecar
	sts := desiredStatefulSet(testInstance(), cfg, "repo/anki:1", 1, "")
	pod := sts.Spec.Template.Spec

	// One app container (anki); the sidecar is a NATIVE sidecar => an initContainer.
	if len(pod.Containers) != 1 || pod.Containers[0].Name != "anki" {
		t.Fatalf("want 1 app container 'anki', got %d: %+v", len(pod.Containers), pod.Containers)
	}
	if len(pod.InitContainers) != 1 {
		t.Fatalf("want 1 init/sidecar container, got %d", len(pod.InitContainers))
	}
	side := pod.InitContainers[0]

	if side.Name != config.SidecarContainerName {
		t.Errorf("sidecar name = %q, want %q", side.Name, config.SidecarContainerName)
	}
	if side.RestartPolicy == nil || *side.RestartPolicy != corev1.ContainerRestartPolicyAlways {
		t.Errorf("sidecar RestartPolicy = %v, want Always (native sidecar)", side.RestartPolicy)
	}
	// Bidirectional propagation REQUIRES privileged (verified against k8s validation).
	if side.SecurityContext == nil || side.SecurityContext.Privileged == nil || !*side.SecurityContext.Privileged {
		t.Errorf("sidecar must be privileged (Bidirectional propagation requires it)")
	}
	// Never combine privileged:true with allowPrivilegeEscalation:false (API rejects it).
	if side.SecurityContext.AllowPrivilegeEscalation != nil {
		t.Errorf("sidecar must not set allowPrivilegeEscalation alongside privileged:true")
	}
	// The sidecar's media mount must be Bidirectional; anki's must be HostToContainer.
	sideMedia := findVolumeMount(side.VolumeMounts, config.MediaVolume)
	if sideMedia == nil || sideMedia.MountPropagation == nil || *sideMedia.MountPropagation != corev1.MountPropagationBidirectional {
		t.Errorf("sidecar media mount propagation = %v, want Bidirectional", sideMedia)
	}
	// Cache on the PVC via subPath.
	cache := findVolumeMount(side.VolumeMounts, config.ProfileVolume)
	if cache == nil || cache.MountPath != config.RcloneCacheMount || cache.SubPath != config.RcloneCacheSubPath {
		t.Errorf("sidecar cache mount = %v, want %s subPath %s", cache, config.RcloneCacheMount, config.RcloneCacheSubPath)
	}
	// B2 creds via envFrom the SEPARATE -b2 Secret, optional.
	if len(side.EnvFrom) != 1 || side.EnvFrom[0].SecretRef == nil ||
		side.EnvFrom[0].SecretRef.Name != b2SecretName(testInstance()) ||
		side.EnvFrom[0].SecretRef.Optional == nil || !*side.EnvFrom[0].SecretRef.Optional {
		t.Errorf("sidecar envFrom = %+v, want optional secretRef %s", side.EnvFrom, b2SecretName(testInstance()))
	}
	// SECRET SCOPE: the credentials Secret (which holds the hkey) must NEVER be
	// envFrom'd into ANY container — that would leak the hkey into the sidecar env.
	for _, c := range append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...) {
		for _, ef := range c.EnvFrom {
			if ef.SecretRef != nil && ef.SecretRef.Name == childName(testInstance()) {
				t.Errorf("container %q envFroms the credentials Secret %q — hkey leak", c.Name, childName(testInstance()))
			}
		}
	}
	if e := findEnv(side.Env, config.EnvContainerRole); e == nil || e.Value != config.ContainerRoleSidecar {
		t.Errorf("sidecar CONTAINER_ROLE = %v, want %s", e, config.ContainerRoleSidecar)
	}

	// anki container: unprivileged, MEDIA_MOUNT_MODE=external, media mount HostToContainer.
	anki := pod.Containers[0]
	if anki.SecurityContext.Privileged != nil {
		t.Errorf("anki container must not set privileged")
	}
	if anki.SecurityContext.AllowPrivilegeEscalation == nil || *anki.SecurityContext.AllowPrivilegeEscalation {
		t.Errorf("anki allowPrivilegeEscalation must be false")
	}
	if len(anki.SecurityContext.Capabilities.Add) != 0 {
		t.Errorf("anki container must add NO capabilities, got %v", anki.SecurityContext.Capabilities.Add)
	}
	if e := findEnv(anki.Env, config.EnvMediaMountMode); e == nil || e.Value != config.ImageMediaModeExternal {
		t.Errorf("anki MEDIA_MOUNT_MODE = %v, want external", e)
	}
	ankiMedia := findVolumeMount(anki.VolumeMounts, config.MediaVolume)
	if ankiMedia == nil || ankiMedia.MountPropagation == nil || *ankiMedia.MountPropagation != corev1.MountPropagationHostToContainer {
		t.Errorf("anki media mount propagation = %v, want HostToContainer", ankiMedia)
	}
	// Both containers wire the preStop drain hook.
	if anki.Lifecycle == nil || anki.Lifecycle.PreStop == nil {
		t.Errorf("anki container must wire a preStop hook")
	}
	if side.Lifecycle == nil || side.Lifecycle.PreStop == nil {
		t.Errorf("sidecar must wire a preStop hook")
	}
	// The shared media emptyDir must exist.
	var mediaVol *corev1.Volume
	for i := range pod.Volumes {
		if pod.Volumes[i].Name == config.MediaVolume {
			mediaVol = &pod.Volumes[i]
		}
	}
	if mediaVol == nil || mediaVol.EmptyDir == nil {
		t.Errorf("want a shared media emptyDir volume, got %+v", pod.Volumes)
	}
}

func TestDesiredStatefulSet_ExternalMode(t *testing.T) {
	cfg := config.Default()
	cfg.MediaMountMode = config.MediaMountModeExternal
	sts := desiredStatefulSet(testInstance(), cfg, "repo/anki:1", 1, "")
	pod := sts.Spec.Template.Spec

	// External mode: NO sidecar, NO shared media volume (the CSI driver provides the
	// mount out-of-band). The pod stays a single unprivileged container.
	if len(pod.InitContainers) != 0 {
		t.Errorf("external mode must render no sidecar, got %d init containers", len(pod.InitContainers))
	}
	if len(pod.Containers) != 1 {
		t.Fatalf("want 1 container, got %d", len(pod.Containers))
	}
	anki := pod.Containers[0]
	// Still told it is not the mounter.
	if e := findEnv(anki.Env, config.EnvMediaMountMode); e == nil || e.Value != config.ImageMediaModeExternal {
		t.Errorf("anki MEDIA_MOUNT_MODE = %v, want external", e)
	}
	// No shared media volume/mount in external mode.
	if findVolumeMount(anki.VolumeMounts, config.MediaVolume) != nil {
		t.Errorf("external mode must not mount the shared media volume in the anki container")
	}
	for i := range pod.Volumes {
		if pod.Volumes[i].Name == config.MediaVolume {
			t.Errorf("external mode must not add the shared media volume")
		}
	}
	// Unprivileged, no added caps.
	if anki.SecurityContext.Privileged != nil {
		t.Errorf("anki container must not be privileged")
	}
	if len(anki.SecurityContext.Capabilities.Add) != 0 {
		t.Errorf("anki container must add no capabilities")
	}
}

func TestDesiredStatefulSet_RestartedAtAnnotation(t *testing.T) {
	cfg := config.Default()

	// Empty restartedAt: NO annotation on the pod template (absent field = no
	// annotation, no churn — requirements-operator §3.1).
	sts := desiredStatefulSet(testInstance(), cfg, "repo/anki:1", 1, "")
	if _, ok := sts.Spec.Template.Annotations[config.AnnotationRestartedAt]; ok {
		t.Errorf("empty restartedAt must render no annotation, got %v", sts.Spec.Template.Annotations)
	}

	// Set: the value is copied VERBATIM as the template annotation, and nothing
	// else about the rendered object changes (envtest case 8b's unit-level half).
	const nonce = "2026-07-11T12:00:00Z"
	stamped := desiredStatefulSet(testInstance(), cfg, "repo/anki:1", 1, nonce)
	if got := stamped.Spec.Template.Annotations[config.AnnotationRestartedAt]; got != nonce {
		t.Errorf("template annotation %s = %q, want %q (verbatim)", config.AnnotationRestartedAt, got, nonce)
	}
	stamped.Spec.Template.Annotations = nil
	if !apiequality.Semantic.DeepEqual(sts, stamped) {
		t.Errorf("restartedAt must change ONLY the template annotation; other rendered fields differ")
	}
}

func TestResolveRestartedAt(t *testing.T) {
	const nonce = "2026-07-11T12:00:00Z"
	withAnnotation := func(v string) *appsv1.StatefulSet {
		sts := stsWithImage("repo/anki:1")
		sts.Spec.Template.Annotations = map[string]string{config.AnnotationRestartedAt: v}
		return sts
	}
	specWith := func(v string) *ankiv1alpha1.AnkiInstance {
		inst := testInstance()
		inst.Spec.RestartedAt = v
		return inst
	}

	tests := []struct {
		name string
		inst *ankiv1alpha1.AnkiInstance
		live *appsv1.StatefulSet
		want string
	}{
		{"spec set, no live STS -> spec wins", specWith(nonce), nil, nonce},
		{"spec set overrides a live annotation", specWith(nonce), withAnnotation("2026-01-01T00:00:00Z"), nonce},
		{"never set, no live STS -> empty", specWith(""), nil, ""},
		{"never set, live without annotation -> empty", specWith(""), stsWithImage("repo/anki:1"), ""},
		// Clearing the field must NOT roll the pod: carry the live value forward
		// so the template hash is unchanged (envtest case 8b).
		{"cleared -> live annotation carried forward", specWith(""), withAnnotation(nonce), nonce},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := resolveRestartedAt(tc.inst, tc.live); got != tc.want {
				t.Fatalf("resolveRestartedAt() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestConfigValidate_MediaMountMode(t *testing.T) {
	cfg := config.Default()
	cfg.MediaMountMode = "bogus"
	if err := cfg.Validate(); err == nil {
		t.Errorf("expected validation error for bogus media-mount-mode")
	}
	for _, m := range []string{config.MediaMountModeSidecar, config.MediaMountModeExternal} {
		cfg.MediaMountMode = m
		if err := cfg.Validate(); err != nil {
			t.Errorf("mode %q should validate, got %v", m, err)
		}
	}
}
