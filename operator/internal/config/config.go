// Package config holds the operator-level (not per-CR) configuration surface
// described in requirements-operator §6. Everything here has a sane default and
// is overridable by flag; nothing here is security-sensitive (the operator holds
// no credentials).
package config

import (
	"flag"
	"fmt"

	"k8s.io/apimachinery/pkg/api/resource"
)

// Contract constants (docs/contracts.md). These are shared cross-component values
// and are intentionally NOT flags: changing them is a contract change, not config.
const (
	// FieldOwner is the Server-Side Apply field manager the operator uses for all
	// children so it never fights the activator's spec.replicas write or the
	// lifecycle service's spec writes (docs/contracts.md §2, requirements-operator §5.3).
	FieldOwner = "anki-operator"

	// ChildNamePrefix is prepended to the keycloakId to name children: anki-<keycloakId>.
	ChildNamePrefix = "anki-"

	// Finalizer implements the PVC data-fate flow (requirements-operator §5.4): on
	// CR delete with spec.dataRetention: Delete the operator deletes the per-user
	// PVC before releasing; with Retain (default) it releases without touching it.
	Finalizer = "anki.ankimcp.ai/finalizer"

	// AnnotationRestartedAt is the pod-template annotation the operator stamps with
	// spec.restartedAt (verbatim) — the `kubectl rollout restart` pattern
	// (requirements-operator §3.1): a changed value changes the template hash, so
	// the StatefulSet controller recreates the pod while the PVC is untouched.
	AnnotationRestartedAt = "anki.ankimcp.ai/restartedAt"

	// Common labels (docs/contracts.md §5).
	LabelName      = "app.kubernetes.io/name"
	LabelManagedBy = "app.kubernetes.io/managed-by"
	LabelInstance  = "app.kubernetes.io/instance"
	LabelUser      = "anki.ankimcp.ai/user"

	LabelNameValue      = "anki-instance"
	LabelManagedByValue = "anki-operator"

	// Ports (docs/contracts.md §7).
	PortMCPName   = "mcp"
	PortMCP       = 3141
	PortVNCWSName = "vnc-ws"
	PortVNCWS     = 6080

	// Credentials Secret mount (docs/contracts.md §8, REQUIREMENTS §B.2).
	SecretMountDir = "/run/ankimcp"
	SecretVolume   = "sync-credentials"
	SecretMode     = int32(0o400)
	ProfileVolume  = "profile"
	ProfileMount   = "/data"

	// SecretSuffixB2 names the SECOND per-user Secret, anki-<keycloakId>-b2
	// (docs/contracts.md §8/§9). It holds ONLY the B2/rclone env creds
	// (RCLONE_CONFIG_B2_* + B2_BUCKET/B2_PREFIX) and is envFrom'd into the rclone
	// sidecar ONLY. The credentials Secret (anki-<keycloakId>, holding the hkey) is
	// NEVER envFrom'd anywhere — it is file-mounted into the anki container only —
	// so the hkey never leaks into the privileged sidecar's environment. Both are
	// written by the lifecycle service.
	SecretSuffixB2 = "-b2"

	// --- Media mount modes (--media-mount-mode; ARCHITECTURE §10 layer 3,
	// docs/contracts.md pod shape, requirements-headless-anki-image §5.3) ---
	//
	// MediaMountModeSidecar (default): render a per-user rclone SIDECAR container
	// that FUSE-mounts the user's B2 prefix into a shared emptyDir; the anki
	// container reads it via mount propagation and stays unprivileged. The sidecar
	// is `privileged: true` (see the sidecar builder for the honest reason —
	// Bidirectional propagation requires it), so the anki-instances namespace must
	// be PSA-`privileged` (docs/contracts.md §4).
	//
	// MediaMountModeExternal: the media mount is provided out-of-band (a CSI
	// driver / DaemonSet); the operator renders ONLY the anki container with
	// MEDIA_MOUNT_MODE=external. The CSI volume itself is not synthesised here —
	// this mode keeps the pod PSA-baseline and exists so the CSI path stays
	// possible (ARCHITECTURE §12 item 9).
	MediaMountModeSidecar  = "sidecar"
	MediaMountModeExternal = "external"

	// Image env contract consumed by the pod image (requirements-headless-anki-image
	// §5.3/§12). The anki container always runs with MEDIA_MOUNT_MODE=external in
	// both operator modes (it never runs rclone itself); the sidecar runs the image
	// with CONTAINER_ROLE=rclone-sidecar + MEDIA_MOUNT_MODE=internal.
	EnvMediaMountMode      = "MEDIA_MOUNT_MODE"
	EnvContainerRole       = "CONTAINER_ROLE"
	ImageMediaModeExternal = "external"
	ImageMediaModeInternal = "internal"
	ContainerRoleSidecar   = "rclone-sidecar"

	// Shared media volume (emptyDir): the rclone sidecar mounts it Bidirectional
	// and creates its FUSE mount inside it; the anki container mounts it
	// HostToContainer so the mount appears there via propagation.
	MediaVolume    = "media"
	MediaMountPath = "/media/b2"

	// rclone VFS cache stays on the user's PVC (ARCHITECTURE §4 durability): the
	// sidecar mounts this subPath of the profile PVC at the image's cache dir.
	RcloneCacheSubPath = ".rclone-cache"
	RcloneCacheMount   = "/data/.rclone-cache"

	// SidecarContainerName is the rclone sidecar's container name.
	SidecarContainerName = "rclone"

	// PreStopScript is the drain-capable graceful-shutdown hook the image ships;
	// the operator wires it as lifecycle.preStop on BOTH containers (the anki
	// container quits Anki + drains via the sidecar's rc over pod-shared
	// localhost; the sidecar drains + unmounts). requirements-headless-anki-image §7.
	PreStopScript = "/opt/ankimcp/bin/prestop.sh"
)

// Config is the resolved operator configuration.
type Config struct {
	// Namespace is where every AnkiInstance CR and its children live.
	Namespace string

	// FleetImage is the pod image all instances converge to unless spec.image
	// overrides it (requirements-operator §7). Required in production.
	FleetImage string

	// StorageClassName / PVCSize configure the per-user PVC (docs/contracts.md §6).
	StorageClassName string
	PVCSize          resource.Quantity

	// Node affinity target (docs/contracts.md §6). Infra must create this node label.
	NodeLabelKey   string
	NodeLabelValue string

	// MediaMountMode selects how the per-user B2 media mount reaches the pod:
	// "sidecar" (default) or "external" — see the const block for the full contract.
	MediaMountMode string

	// Sidecar (rclone) pod sizing. rclone is light; the VFS cache is on disk (the
	// PVC), so RAM stays modest. Only used when MediaMountMode == "sidecar".
	SidecarCPURequest    resource.Quantity
	SidecarCPULimit      resource.Quantity
	SidecarMemoryRequest resource.Quantity
	SidecarMemoryLimit   resource.Quantity

	// Pod sizing.
	CPURequest    resource.Quantity
	CPULimit      resource.Quantity
	MemoryRequest resource.Quantity
	MemoryLimit   resource.Quantity

	// TerminationGracePeriodSeconds covers graceful quit + rclone drain (ARCHITECTURE §5).
	//
	// INVARIANT (drift risk — the timeouts live in the image's scripts/lib.sh, the
	// grace lives here): grace >= ANKI_QUIT_TIMEOUT + 2*RCLONE_DRAIN_TIMEOUT + 30.
	// The preStop budget is sequential across the native-sidecar teardown: the anki
	// container quits Anki (ANKI_QUIT_TIMEOUT=60) and drains (RCLONE_DRAIN_TIMEOUT=45),
	// THEN the sidecar drains again + unmounts (RCLONE_DRAIN_TIMEOUT=45). Worst case
	// ~150s; kubelet SIGKILLs at grace, truncating the drain exactly when Anki stalls
	// on a sync dialog. Default 180 = 150 + margin. Keep in sync with lib.sh.
	TerminationGracePeriodSeconds int64

	// Pod identity. The exact uid/gid are IMAGE-DEFINED and PINNED at 10001/10001 by
	// the headless-anki image (Dockerfile ANKI_UID/ANKI_GID + usermod). These
	// operator defaults MUST equal that pin: the pod securityContext overrides the
	// image, so a mismatch makes everything run as the wrong uid — the 0400
	// credentials Secret (chowned to fsGroup by the kubelet) becomes unreadable and
	// FUSE `--allow-other` ownership breaks. RunAsUser = the image's anki uid;
	// FSGroup = the image's anki gid (runAsNonRoot is enforced). Overridable by flag
	// but keep aligned with the image pin (docs/contracts.md §8).
	RunAsUser int64
	FSGroup   int64

	// Rollout controls (requirements-operator §7).
	RolloutOnlyWhenSuspended bool
	RolloutMaxConcurrent     int
	RolloutPaused            bool

	// MaxConcurrentReconciles tunes controller parallelism (requirements-operator §5.1).
	MaxConcurrentReconciles int

	// MetricsInterval is how often the fleet-metrics runnable resamples (§7 observability).
	MetricsIntervalSeconds int
}

// Default returns a Config populated with the documented defaults
// (requirements-operator §6). FleetImage is left empty and is required.
func Default() *Config {
	return &Config{
		Namespace:                     "anki-instances",
		FleetImage:                    "",
		StorageClassName:              "hcloud-volumes",
		PVCSize:                       resource.MustParse("10Gi"),
		NodeLabelKey:                  "ankimcp.ai/storage",
		NodeLabelValue:                "hcloud-volumes",
		MediaMountMode:                MediaMountModeSidecar,
		CPURequest:                    resource.MustParse("250m"),
		CPULimit:                      resource.MustParse("1"),
		MemoryRequest:                 resource.MustParse("512Mi"),
		MemoryLimit:                   resource.MustParse("1Gi"),
		SidecarCPURequest:             resource.MustParse("50m"),
		SidecarCPULimit:               resource.MustParse("500m"),
		SidecarMemoryRequest:          resource.MustParse("128Mi"),
		SidecarMemoryLimit:            resource.MustParse("512Mi"),
		TerminationGracePeriodSeconds: 180,
		RunAsUser:                     10001,
		FSGroup:                       10001,
		RolloutOnlyWhenSuspended:      true,
		RolloutMaxConcurrent:          20,
		RolloutPaused:                 false,
		MaxConcurrentReconciles:       10,
		MetricsIntervalSeconds:        30,
	}
}

// BindFlags registers the config surface as flags on the given FlagSet, seeded
// from the defaults already present in c.
func (c *Config) BindFlags(fs *flag.FlagSet) {
	fs.StringVar(&c.Namespace, "namespace", c.Namespace, "Namespace holding AnkiInstance CRs and their children.")
	fs.StringVar(&c.FleetImage, "fleet-image", c.FleetImage, "Pod image all instances converge to unless spec.image overrides it (required).")
	fs.StringVar(&c.StorageClassName, "storage-class", c.StorageClassName, "StorageClass for the per-user PVC.")
	fs.StringVar(&c.NodeLabelKey, "node-label-key", c.NodeLabelKey, "Node-affinity label key for Cloud-Volume-capable nodes.")
	fs.StringVar(&c.NodeLabelValue, "node-label-value", c.NodeLabelValue, "Node-affinity label value.")
	fs.StringVar(&c.MediaMountMode, "media-mount-mode", c.MediaMountMode, "How the B2 media mount reaches the pod: 'sidecar' (per-user privileged rclone sidecar; default) or 'external' (CSI-provided).")
	fs.Int64Var(&c.TerminationGracePeriodSeconds, "termination-grace-period", c.TerminationGracePeriodSeconds, "Pod terminationGracePeriodSeconds.")
	fs.Int64Var(&c.RunAsUser, "run-as-user", c.RunAsUser, "Pod runAsUser (must match the image's anki uid).")
	fs.Int64Var(&c.FSGroup, "fs-group", c.FSGroup, "Pod fsGroup (must match the image's anki gid; makes the 0400 Secret readable).")
	fs.BoolVar(&c.RolloutOnlyWhenSuspended, "rollout-only-when-suspended", c.RolloutOnlyWhenSuspended, "Only re-template a StatefulSet while it is at 0 replicas.")
	fs.IntVar(&c.RolloutMaxConcurrent, "rollout-max-concurrent", c.RolloutMaxConcurrent, "Max StatefulSets out-of-date and actively rolling at once.")
	fs.BoolVar(&c.RolloutPaused, "rollout-paused", c.RolloutPaused, "Freeze all image rollout.")
	fs.IntVar(&c.MaxConcurrentReconciles, "max-concurrent-reconciles", c.MaxConcurrentReconciles, "Controller worker concurrency.")
	fs.IntVar(&c.MetricsIntervalSeconds, "metrics-interval", c.MetricsIntervalSeconds, "Fleet-metrics resample interval, seconds.")

	// resource.Quantity flags via a small adapter.
	fs.Var(quantityValue{&c.PVCSize}, "pvc-size", "Per-user PVC size request.")
	fs.Var(quantityValue{&c.CPURequest}, "cpu-request", "Pod CPU request.")
	fs.Var(quantityValue{&c.CPULimit}, "cpu-limit", "Pod CPU limit.")
	fs.Var(quantityValue{&c.MemoryRequest}, "memory-request", "Pod memory request.")
	fs.Var(quantityValue{&c.MemoryLimit}, "memory-limit", "Pod memory limit.")
}

// Validate checks invariants that flags can't express.
func (c *Config) Validate() error {
	if c.Namespace == "" {
		return fmt.Errorf("namespace must not be empty")
	}
	if c.RolloutMaxConcurrent < 1 {
		return fmt.Errorf("rollout-max-concurrent must be >= 1, got %d", c.RolloutMaxConcurrent)
	}
	switch c.MediaMountMode {
	case MediaMountModeSidecar, MediaMountModeExternal:
	default:
		return fmt.Errorf("media-mount-mode must be %q or %q, got %q",
			MediaMountModeSidecar, MediaMountModeExternal, c.MediaMountMode)
	}
	// FleetImage is intentionally not required here so `make run`/tests without a
	// real image still start; an instance with neither spec.image nor a fleet image
	// is surfaced as an error at reconcile time instead of crashing the manager.
	return nil
}

// quantityValue adapts resource.Quantity to flag.Value.
type quantityValue struct{ q *resource.Quantity }

func (v quantityValue) String() string {
	if v.q == nil {
		return ""
	}
	return v.q.String()
}

func (v quantityValue) Set(s string) error {
	q, err := resource.ParseQuantity(s)
	if err != nil {
		return err
	}
	*v.q = q
	return nil
}
