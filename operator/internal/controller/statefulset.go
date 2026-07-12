package controller

import (
	"context"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// resolveRestartedAt returns the value the restart-nonce pod-template annotation
// should carry (requirements-operator §3.1/§5.2). spec.restartedAt wins when set;
// when it is empty/absent, the live StatefulSet's annotation is carried forward so
// that CLEARING the field never changes the template hash (which would roll the pod
// spuriously — envtest case 8b). A never-set field on a fresh instance yields "".
// Pure; restartedAtValue supplies a genuinely-live `live` on the carry-forward path.
func resolveRestartedAt(instance *ankiv1alpha1.AnkiInstance, live *appsv1.StatefulSet) string {
	if instance.Spec.RestartedAt != "" {
		return instance.Spec.RestartedAt
	}
	if live != nil {
		return live.Spec.Template.Annotations[config.AnnotationRestartedAt]
	}
	return ""
}

// restartedAtValue resolves the restart nonce for the desired StatefulSet. On the
// carry-forward path (spec.restartedAt empty, child exists) the source STS is read
// with the UNCACHED APIReader: the informer cache can lag a set-then-quick-clear
// sequence — the reconcile for the clear may run before the cache has seen the STS
// stamped with the nonce, so a cached read would resolve the stale (or absent)
// annotation, drop the nonce, and roll the pod — exactly what carry-forward exists
// to prevent. The extra live GET only happens on this rare path (field cleared /
// left empty on an existing child); the spec-set and no-child paths never hit the
// apiserver.
func (r *AnkiInstanceReconciler) restartedAtValue(ctx context.Context, instance *ankiv1alpha1.AnkiInstance, cached *appsv1.StatefulSet) (string, error) {
	if instance.Spec.RestartedAt != "" || cached == nil {
		return resolveRestartedAt(instance, cached), nil
	}
	var live appsv1.StatefulSet
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(cached), &live); err != nil {
		if apierrors.IsNotFound(err) {
			// Deleted between the cached read and now: nothing to carry forward.
			return resolveRestartedAt(instance, nil), nil
		}
		return "", err
	}
	return resolveRestartedAt(instance, &live), nil
}

// desiredStatefulSet builds the core child StatefulSet (requirements-operator §4.1,
// ARCHITECTURE §3/§4/§10). image, replicas and restartedAt are resolved by the
// caller (rollout logic decides the image; replicas is the effective value;
// restartedAt via restartedAtValue). The returned object carries no ownerRef;
// the caller sets the controller ref before applying.
func desiredStatefulSet(instance *ankiv1alpha1.AnkiInstance, cfg *config.Config, image string, replicas int32, restartedAt string) *appsv1.StatefulSet {
	name := childName(instance)
	labels := commonLabels(instance)
	retain := appsv1.RetainPersistentVolumeClaimRetentionPolicyType

	// The restart nonce (spec.restartedAt) is copied VERBATIM as a pod-template
	// annotation — the `kubectl rollout restart` pattern. Empty = no annotation.
	templateMeta := metav1.ObjectMeta{Labels: labels}
	if restartedAt != "" {
		templateMeta.Annotations = map[string]string{config.AnnotationRestartedAt: restartedAt}
	}

	sts := &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "StatefulSet",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: instance.Namespace,
			Labels:    labels,
		},
		Spec: appsv1.StatefulSetSpec{
			ServiceName:         name, // the headless Service governs pod DNS identity
			Replicas:            ptr.To(replicas),
			PodManagementPolicy: appsv1.OrderedReadyPodManagement,
			UpdateStrategy:      appsv1.StatefulSetUpdateStrategy{Type: appsv1.RollingUpdateStatefulSetStrategyType},
			// Thousands of STSes: keep controller-revision clutter down.
			RevisionHistoryLimit: ptr.To(int32(1)),
			Selector:             &metav1.LabelSelector{MatchLabels: labels},
			// LOAD-BEARING: Delete would wipe the user's disk on every idle cycle.
			PersistentVolumeClaimRetentionPolicy: &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{
				WhenDeleted: retain,
				WhenScaled:  retain,
			},
			VolumeClaimTemplates: []corev1.PersistentVolumeClaim{
				{
					ObjectMeta: metav1.ObjectMeta{Name: config.ProfileVolume},
					Spec: corev1.PersistentVolumeClaimSpec{
						AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOncePod},
						StorageClassName: ptr.To(cfg.StorageClassName),
						Resources: corev1.VolumeResourceRequirements{
							Requests: corev1.ResourceList{corev1.ResourceStorage: cfg.PVCSize},
						},
					},
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: templateMeta,
				Spec:       podSpec(instance, cfg, image),
			},
		},
	}
	return sts
}

func podSpec(instance *ankiv1alpha1.AnkiInstance, cfg *config.Config, image string) corev1.PodSpec {
	sidecar := cfg.MediaMountMode == config.MediaMountModeSidecar

	ps := corev1.PodSpec{
		// Layer 2 (ARCHITECTURE §10): the pod carries NO k8s credentials.
		AutomountServiceAccountToken:  ptr.To(false),
		ServiceAccountName:            "anki-pod", // a zero-permission SA (no RoleBindings)
		TerminationGracePeriodSeconds: ptr.To(cfg.TerminationGracePeriodSeconds),
		// POD-level securityContext (layer 3). This is the DEFAULT for every
		// container; the anki container inherits it wholesale (unprivileged, non-root,
		// drop-ALL). The rclone sidecar's CONTAINER-level securityContext overrides
		// only what FUSE forces (privileged) — see rcloneSidecar().
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot:   ptr.To(true),
			RunAsUser:      ptr.To(cfg.RunAsUser),
			FSGroup:        ptr.To(cfg.FSGroup),
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
		},
		// Cloud Volumes cannot attach to Hetzner dedicated servers -> pin to the
		// storage-capable nodes (docs/contracts.md §6). Infra must create the label.
		Affinity: &corev1.Affinity{
			NodeAffinity: &corev1.NodeAffinity{
				RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
					NodeSelectorTerms: []corev1.NodeSelectorTerm{
						{
							MatchExpressions: []corev1.NodeSelectorRequirement{
								{
									Key:      cfg.NodeLabelKey,
									Operator: corev1.NodeSelectorOpIn,
									Values:   []string{cfg.NodeLabelValue},
								},
							},
						},
					},
				},
			},
		},
		Containers: []corev1.Container{ankiContainer(cfg, image, sidecar)},
		Volumes:    []corev1.Volume{secretVolume(instance)},
	}

	if sidecar {
		// The shared media emptyDir: the rclone sidecar creates its FUSE mount
		// inside it (Bidirectional propagation) and the anki container sees it
		// appear (HostToContainer). ARCHITECTURE §10 layer 3, docs/contracts.md.
		ps.Volumes = append(ps.Volumes, corev1.Volume{
			Name:         config.MediaVolume,
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
		// NATIVE sidecar (init container with restartPolicy: Always): it starts —
		// and mounts — BEFORE the anki container, and is terminated AFTER it, so
		// rclone stays alive while Anki's preStop close-syncs and drains through the
		// sidecar's rc over pod-shared localhost. GA k8s 1.33 / on-by-default since
		// 1.29 (cluster targets 1.36). This deterministic ordering is what makes the
		// preStop drain correct without a fragile cross-container race.
		ps.InitContainers = []corev1.Container{rcloneSidecar(instance, cfg, image)}
	}

	return ps
}

// ankiContainer builds the customer-facing Anki container. It ALWAYS runs
// unprivileged (inherits the pod securityContext, drops ALL caps, no privilege
// escalation) and never runs rclone itself: MEDIA_MOUNT_MODE=external means it
// waits for the media mount to be provided (by the sidecar, or by a CSI driver in
// external mode) before opening the collection (requirements-headless-anki-image
// §5.3/§6).
func ankiContainer(cfg *config.Config, image string, sidecar bool) corev1.Container {
	mounts := []corev1.VolumeMount{
		{Name: config.ProfileVolume, MountPath: config.ProfileMount},
		// Whole-volume Secret mount, NEVER subPath (subPath mounts never refresh,
		// which would silently kill credential rotation).
		{Name: config.SecretVolume, MountPath: config.SecretMountDir, ReadOnly: true},
	}
	if sidecar {
		// Receive the sidecar's FUSE mount via propagation. HostToContainer (rslave)
		// is the minimum that lets a mount created elsewhere appear here; the anki
		// container needs NO extra privilege for this (only the sidecar, which does
		// the outward-propagating Bidirectional mount, must be privileged).
		mounts = append(mounts, corev1.VolumeMount{
			Name:             config.MediaVolume,
			MountPath:        config.MediaMountPath,
			MountPropagation: ptr.To(corev1.MountPropagationHostToContainer),
		})
	}

	return corev1.Container{
		Name:  "anki",
		Image: image,
		Env: []corev1.EnvVar{
			// The anki container never runs rclone in either operator mode; it waits
			// for the externally-provided media mount (sidecar or CSI).
			{Name: config.EnvMediaMountMode, Value: config.ImageMediaModeExternal},
		},
		// CONTAINER-level securityContext (layer 3): unprivileged, drop ALL, no
		// privilege escalation. Deliberately NO added capabilities — the FUSE
		// privilege is confined to the rclone sidecar.
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			// readOnlyRootFilesystem left unset (false): the image needs a writable
			// rootfs for the desktop/Anki/Qt runtime. Flip to true if the image tolerates it.
			Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    cfg.CPURequest,
				corev1.ResourceMemory: cfg.MemoryRequest,
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    cfg.CPULimit,
				corev1.ResourceMemory: cfg.MemoryLimit,
			},
		},
		Ports: []corev1.ContainerPort{
			{Name: config.PortMCPName, ContainerPort: config.PortMCP, Protocol: corev1.ProtocolTCP},
			{Name: config.PortVNCWSName, ContainerPort: config.PortVNCWS, Protocol: corev1.ProtocolTCP},
		},
		VolumeMounts: mounts,
		// preStop: quit Anki cleanly (close-sync) then drain the rclone write-back
		// queue via the sidecar's rc over pod-shared localhost (the sidecar outlives
		// this container — native-sidecar ordering). requirements-headless-anki-image §7.
		Lifecycle: preStopLifecycle(),
		// The readiness probe is the image's job (it must reflect "Anki up + media
		// mount live + MCP listening"). The operator does not synthesize one; the
		// activator holds the request until the pod reports Ready.
	}
}

// rcloneSidecar builds the per-user rclone FUSE sidecar (ARCHITECTURE §10 layer 3,
// DECIDED 2026-07-10). It runs the SAME pod image with CONTAINER_ROLE=rclone-sidecar
// so the image's entrypoint runs ONLY the rclone mount (+ its localhost rc + a
// drain-capable preStop).
//
// PRIVILEGE — the honest story (verified against k8s docs + kubernetes/kubernetes
// PR #117812, closed unmerged 2024): the sidecar mounts the shared media emptyDir
// with mountPropagation: Bidirectional so its FUSE mount propagates out to the anki
// container. Kubernetes REQUIRES a container using Bidirectional propagation to be
// `privileged: true` — `SYS_ADMIN` + `/dev/fuse` alone are NOT accepted by API
// validation (the proposal to relax this was closed without merging). So the
// aspirational "drop ALL + add SYS_ADMIN + /dev/fuse, privileged:false" shape is
// NOT achievable for an outward-propagating in-pod FUSE mount; the sidecar is
// `privileged: true`. This is the accepted cost that makes the anki-instances
// namespace PSA-`privileged` (docs/contracts.md §4). The blast radius is bounded:
// the sidecar still runs as the non-root anki uid, holds no k8s creds, and the
// anki container stays fully unprivileged.
func rcloneSidecar(instance *ankiv1alpha1.AnkiInstance, cfg *config.Config, image string) corev1.Container {
	return corev1.Container{
		Name:  config.SidecarContainerName,
		Image: image,
		// restartPolicy: Always turns this init container into a NATIVE SIDECAR
		// (start-first / terminate-last). Ordering is what makes the drain correct.
		RestartPolicy: ptr.To(corev1.ContainerRestartPolicyAlways),
		Env: []corev1.EnvVar{
			{Name: config.EnvContainerRole, Value: config.ContainerRoleSidecar},
			// The sidecar itself runs the in-image ("internal") rclone mount.
			{Name: config.EnvMediaMountMode, Value: config.ImageMediaModeInternal},
		},
		// B2 credentials for the sidecar: the per-user, prefix-scoped rclone config
		// (RCLONE_CONFIG_B2_* keys + B2_BUCKET/B2_PREFIX) is delivered as env FROM a
		// SEPARATE per-user Secret, anki-<keycloakId>-b2 (docs/contracts.md §8/§9),
		// written by the lifecycle service. envFrom is backend-agnostic: the lifecycle
		// service decides the native-`b2` vs `s3`-at-B2 key set (still open,
		// docs/contracts.md open decision #5) and this picks up whatever it wrote.
		// optional:true so a not-yet-provisioned Secret doesn't wedge start.
		//
		// SECRET SCOPE (do not regress): the credentials Secret anki-<keycloakId>
		// (which holds the hkey under `sync-credentials.json`) is NOT envFrom'd here.
		// The kubelet does NOT skip that key — dots/hyphens are valid env-name chars in
		// k8s apimachinery validation — so envFrom'ing it would inject the hkey JSON
		// straight into this privileged sidecar's environment. That Secret is
		// file-mounted into the anki container ONLY; the sidecar reads only -b2.
		EnvFrom: []corev1.EnvFromSource{
			{SecretRef: &corev1.SecretEnvSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: b2SecretName(instance)},
				Optional:             ptr.To(true),
			}},
		},
		SecurityContext: &corev1.SecurityContext{
			// REQUIRED for Bidirectional mount propagation (see the doc comment). Do
			// NOT also set allowPrivilegeEscalation:false here — the API rejects
			// privileged:true combined with allowPrivilegeEscalation:false. runAsUser
			// is inherited from the pod securityContext (the non-root anki uid).
			Privileged: ptr.To(true),
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    cfg.SidecarCPURequest,
				corev1.ResourceMemory: cfg.SidecarMemoryRequest,
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    cfg.SidecarCPULimit,
				corev1.ResourceMemory: cfg.SidecarMemoryLimit,
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			// The FUSE mount lands here and propagates OUT to the anki container.
			{
				Name:             config.MediaVolume,
				MountPath:        config.MediaMountPath,
				MountPropagation: ptr.To(corev1.MountPropagationBidirectional),
			},
			// rclone VFS cache on the user's PVC (ARCHITECTURE §4): a crash before
			// write-back flush must leave the pending file on durable disk. subPath
			// keeps the sidecar from seeing the SQLite DBs on the rest of the PVC.
			// (subPath's no-refresh caveat is a Secret/ConfigMap concern, irrelevant
			// for a PVC.)
			{
				Name:      config.ProfileVolume,
				MountPath: config.RcloneCacheMount,
				SubPath:   config.RcloneCacheSubPath,
			},
		},
		// preStop: a final idempotent rclone drain + clean unmount. By the time k8s
		// terminates the sidecar (after the anki container has fully stopped) the
		// anki preStop has already drained, so this is belt-and-suspenders.
		Lifecycle: preStopLifecycle(),
	}
}

// preStopLifecycle returns the shared preStop hook (the image's drain-capable
// script). It is role-aware inside the image: in the anki container it quits Anki
// then drains; in the sidecar it drains then unmounts (requirements-headless-anki-image §7).
func preStopLifecycle() *corev1.Lifecycle {
	return &corev1.Lifecycle{
		PreStop: &corev1.LifecycleHandler{
			Exec: &corev1.ExecAction{Command: []string{config.PreStopScript}},
		},
	}
}

func secretVolume(instance *ankiv1alpha1.AnkiInstance) corev1.Volume {
	return corev1.Volume{
		Name: config.SecretVolume,
		VolumeSource: corev1.VolumeSource{
			Secret: &corev1.SecretVolumeSource{
				SecretName: childName(instance), // anki-<keycloakId>; lifecycle writes it
				// A not-yet-created Secret must NOT wedge pod start.
				Optional: ptr.To(true),
				// 0400, readable by the anki uid via fsGroup (REQUIREMENTS §B.2).
				DefaultMode: ptr.To(config.SecretMode),
			},
		},
	}
}
