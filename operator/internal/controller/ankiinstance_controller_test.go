package controller

import (
	"fmt"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/uuid"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

const (
	// timeout is deliberately generous: envtest apiserver + a cold cache can make
	// the first reconcile of a spec take several seconds under a loaded CI box, and
	// a tight bound is the classic source of "flaky" controller tests. 30s bounds a
	// real hang without racing a slow-but-correct reconcile.
	timeout  = 30 * time.Second
	interval = 200 * time.Millisecond
)

// newUser returns a fresh, globally-unique DNS-1123-safe keycloakId per spec. Each
// spec gets its own UUID so specs never collide on a shared object name — a noisy or
// randomized (`-ginkgo.randomize-all`) run can never cross-contaminate specs the way
// a shared incrementing counter could.
func newUser() string {
	return fmt.Sprintf("user-%s", uuid.NewUUID())
}

func createInstance(name string, replicas *int32, suspended bool, image string) *ankiv1alpha1.AnkiInstance {
	inst := &ankiv1alpha1.AnkiInstance{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testNS},
		Spec: ankiv1alpha1.AnkiInstanceSpec{
			User:      name,
			Replicas:  replicas,
			Suspended: suspended,
			Image:     image,
		},
	}
	Expect(k8sClient.Create(ctx, inst)).To(Succeed())
	return inst
}

func getSTS(name string) *appsv1.StatefulSet {
	sts := &appsv1.StatefulSet{}
	Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: config.ChildNamePrefix + name}, sts)).To(Succeed())
	return sts
}

// stsReplicas returns the child STS replicas, or an error while it does not yet
// exist — so it is safe to poll inside Eventually (unlike getSTS, which asserts).
func stsReplicas(name string) (int32, error) {
	sts := &appsv1.StatefulSet{}
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: config.ChildNamePrefix + name}, sts); err != nil {
		return -1, err
	}
	if sts.Spec.Replicas == nil {
		return -1, nil
	}
	return *sts.Spec.Replicas, nil
}

var _ = Describe("AnkiInstance controller", func() {

	Context("creating a CR", func() {
		It("creates a StatefulSet and headless Service with the right spec, labels, ownerRef and Secret mount", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(1)), false, "")
			child := config.ChildNamePrefix + name

			By("the StatefulSet appears")
			sts := &appsv1.StatefulSet{}
			Eventually(func() error {
				return k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: child}, sts)
			}, timeout, interval).Should(Succeed())

			By("it carries the controller ownerRef")
			Expect(sts.OwnerReferences).To(HaveLen(1))
			Expect(sts.OwnerReferences[0].Kind).To(Equal("AnkiInstance"))
			Expect(sts.OwnerReferences[0].Controller).To(HaveValue(BeTrue()))
			Expect(sts.OwnerReferences[0].BlockOwnerDeletion).To(HaveValue(BeTrue()))

			By("contract labels")
			Expect(sts.Labels).To(HaveKeyWithValue(config.LabelName, config.LabelNameValue))
			Expect(sts.Labels).To(HaveKeyWithValue(config.LabelManagedBy, config.LabelManagedByValue))
			Expect(sts.Labels).To(HaveKeyWithValue(config.LabelInstance, child))
			Expect(sts.Labels).To(HaveKeyWithValue(config.LabelUser, name))

			By("effective replicas = 1")
			Expect(sts.Spec.Replicas).To(HaveValue(Equal(int32(1))))

			By("image resolved to the fleet image")
			Expect(stsImage(sts)).To(Equal(fleetImage))

			By("RWOP + Retain policy on the volumeClaimTemplate")
			Expect(sts.Spec.VolumeClaimTemplates).To(HaveLen(1))
			vct := sts.Spec.VolumeClaimTemplates[0]
			Expect(vct.Spec.AccessModes).To(ConsistOf(corev1.ReadWriteOncePod))
			Expect(sts.Spec.PersistentVolumeClaimRetentionPolicy.WhenScaled).To(Equal(appsv1.RetainPersistentVolumeClaimRetentionPolicyType))
			Expect(sts.Spec.PersistentVolumeClaimRetentionPolicy.WhenDeleted).To(Equal(appsv1.RetainPersistentVolumeClaimRetentionPolicyType))

			By("pod hardening: automount off, runAsNonRoot, drop ALL, seccomp, node affinity")
			pod := sts.Spec.Template.Spec
			Expect(pod.AutomountServiceAccountToken).To(HaveValue(BeFalse()))
			Expect(pod.SecurityContext.RunAsNonRoot).To(HaveValue(BeTrue()))
			// uid/gid MUST match the headless-anki image pin (10001) or the 0400
			// credentials Secret is unreadable + FUSE ownership breaks (contracts.md §8).
			Expect(pod.SecurityContext.RunAsUser).To(HaveValue(Equal(int64(10001))))
			Expect(pod.SecurityContext.FSGroup).To(HaveValue(Equal(int64(10001))))
			Expect(pod.SecurityContext.SeccompProfile.Type).To(Equal(corev1.SeccompProfileTypeRuntimeDefault))
			Expect(pod.Containers[0].SecurityContext.AllowPrivilegeEscalation).To(HaveValue(BeFalse()))
			Expect(pod.Containers[0].SecurityContext.Capabilities.Drop).To(ConsistOf(corev1.Capability("ALL")))
			na := pod.Affinity.NodeAffinity.RequiredDuringSchedulingIgnoredDuringExecution.NodeSelectorTerms[0].MatchExpressions[0]
			Expect(na.Key).To(Equal("ankimcp.ai/storage"))
			Expect(na.Values).To(ConsistOf("hcloud-volumes"))

			By("container ports mcp/3141 + vnc-ws/6080")
			ports := pod.Containers[0].Ports
			Expect(ports).To(ContainElement(corev1.ContainerPort{Name: "mcp", ContainerPort: 3141, Protocol: corev1.ProtocolTCP}))
			Expect(ports).To(ContainElement(corev1.ContainerPort{Name: "vnc-ws", ContainerPort: 6080, Protocol: corev1.ProtocolTCP}))

			By("whole-volume Secret mount, optional, 0400, never subPath")
			var secretVol *corev1.Volume
			for i := range pod.Volumes {
				if pod.Volumes[i].Name == config.SecretVolume {
					secretVol = &pod.Volumes[i]
				}
			}
			Expect(secretVol).NotTo(BeNil())
			Expect(secretVol.Secret.SecretName).To(Equal(child))
			Expect(secretVol.Secret.Optional).To(HaveValue(BeTrue()))
			Expect(secretVol.Secret.DefaultMode).To(HaveValue(Equal(int32(0o400))))
			var mount *corev1.VolumeMount
			for i := range pod.Containers[0].VolumeMounts {
				if pod.Containers[0].VolumeMounts[i].Name == config.SecretVolume {
					mount = &pod.Containers[0].VolumeMounts[i]
				}
			}
			Expect(mount).NotTo(BeNil())
			Expect(mount.MountPath).To(Equal("/run/ankimcp"))
			Expect(mount.SubPath).To(BeEmpty(), "Secret must be a whole-volume mount, never subPath")

			By("headless Service appears with clusterIP None and ONLY the vnc-ws port (contracts.md §7: mcp 3141 is not a Service port in v1)")
			svc := &corev1.Service{}
			Eventually(func() error {
				return k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: child}, svc)
			}, timeout, interval).Should(Succeed())
			Expect(svc.Spec.ClusterIP).To(Equal(corev1.ClusterIPNone))
			Expect(svc.OwnerReferences).To(HaveLen(1))
			Expect(svc.Spec.Ports).To(HaveLen(1))
			Expect(svc.Spec.Ports[0].Name).To(Equal(config.PortVNCWSName))
			Expect(svc.Spec.Ports[0].Port).To(Equal(int32(config.PortVNCWS)))
		})
	})

	Context("rclone sidecar (default media-mount-mode=sidecar)", func() {
		It("renders a privileged native rclone sidecar, shared media volume with the right propagation, cache-on-PVC, and an unprivileged anki container", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(1)), false, "")
			child := config.ChildNamePrefix + name

			sts := &appsv1.StatefulSet{}
			Eventually(func() error {
				return k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: child}, sts)
			}, timeout, interval).Should(Succeed())
			pod := sts.Spec.Template.Spec

			By("a native sidecar (initContainer restartPolicy: Always) named rclone")
			Expect(pod.InitContainers).To(HaveLen(1))
			side := pod.InitContainers[0]
			Expect(side.Name).To(Equal(config.SidecarContainerName))
			Expect(side.RestartPolicy).To(HaveValue(Equal(corev1.ContainerRestartPolicyAlways)))
			Expect(side.Image).To(Equal(fleetImage))

			By("the sidecar is privileged (Bidirectional propagation requires it) — honestly, not SYS_ADMIN")
			Expect(side.SecurityContext).NotTo(BeNil())
			Expect(side.SecurityContext.Privileged).To(HaveValue(BeTrue()))
			// Must NOT combine privileged:true with allowPrivilegeEscalation:false (API rejects it).
			Expect(side.SecurityContext.AllowPrivilegeEscalation).To(BeNil())

			By("the sidecar runs the image as the rclone-sidecar role, internal mount mode")
			Expect(side.Env).To(ContainElement(corev1.EnvVar{Name: config.EnvContainerRole, Value: config.ContainerRoleSidecar}))
			Expect(side.Env).To(ContainElement(corev1.EnvVar{Name: config.EnvMediaMountMode, Value: config.ImageMediaModeInternal}))

			By("B2 creds arrive via envFrom the SEPARATE -b2 Secret, optional")
			Expect(side.EnvFrom).To(HaveLen(1))
			Expect(side.EnvFrom[0].SecretRef).NotTo(BeNil())
			Expect(side.EnvFrom[0].SecretRef.Name).To(Equal(child + config.SecretSuffixB2))
			Expect(side.EnvFrom[0].SecretRef.Optional).To(HaveValue(BeTrue()))

			By("no container envFroms the credentials Secret (hkey must not leak into the sidecar env)")
			for _, c := range append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...) {
				for _, ef := range c.EnvFrom {
					if ef.SecretRef != nil {
						Expect(ef.SecretRef.Name).NotTo(Equal(child),
							"container %q envFroms the credentials Secret %q — hkey leak", c.Name, child)
					}
				}
			}

			By("the sidecar mounts the shared media volume Bidirectional and the cache on the PVC")
			var sideMedia, sideCache *corev1.VolumeMount
			for i := range side.VolumeMounts {
				switch side.VolumeMounts[i].Name {
				case config.MediaVolume:
					sideMedia = &side.VolumeMounts[i]
				case config.ProfileVolume:
					sideCache = &side.VolumeMounts[i]
				}
			}
			Expect(sideMedia).NotTo(BeNil())
			Expect(sideMedia.MountPath).To(Equal(config.MediaMountPath))
			Expect(sideMedia.MountPropagation).To(HaveValue(Equal(corev1.MountPropagationBidirectional)))
			Expect(sideCache).NotTo(BeNil())
			Expect(sideCache.MountPath).To(Equal(config.RcloneCacheMount))
			Expect(sideCache.SubPath).To(Equal(config.RcloneCacheSubPath))

			By("both containers wire the drain-capable preStop")
			Expect(side.Lifecycle).NotTo(BeNil())
			Expect(side.Lifecycle.PreStop.Exec.Command).To(Equal([]string{config.PreStopScript}))

			By("the anki container is unprivileged: drop ALL, no added caps, not privileged")
			anki := pod.Containers[0]
			Expect(anki.Name).To(Equal("anki"))
			Expect(anki.SecurityContext.Privileged).To(BeNil())
			Expect(anki.SecurityContext.AllowPrivilegeEscalation).To(HaveValue(BeFalse()))
			Expect(anki.SecurityContext.Capabilities.Drop).To(ConsistOf(corev1.Capability("ALL")))
			Expect(anki.SecurityContext.Capabilities.Add).To(BeEmpty())

			By("the anki container runs MEDIA_MOUNT_MODE=external and mounts the shared media HostToContainer")
			Expect(anki.Env).To(ContainElement(corev1.EnvVar{Name: config.EnvMediaMountMode, Value: config.ImageMediaModeExternal}))
			var ankiMedia *corev1.VolumeMount
			for i := range anki.VolumeMounts {
				if anki.VolumeMounts[i].Name == config.MediaVolume {
					ankiMedia = &anki.VolumeMounts[i]
				}
			}
			Expect(ankiMedia).NotTo(BeNil())
			Expect(ankiMedia.MountPropagation).To(HaveValue(Equal(corev1.MountPropagationHostToContainer)))
			Expect(anki.Lifecycle.PreStop.Exec.Command).To(Equal([]string{config.PreStopScript}))

			By("the shared media emptyDir volume exists")
			var mediaVol *corev1.Volume
			for i := range pod.Volumes {
				if pod.Volumes[i].Name == config.MediaVolume {
					mediaVol = &pod.Volumes[i]
				}
			}
			Expect(mediaVol).NotTo(BeNil())
			Expect(mediaVol.EmptyDir).NotTo(BeNil())
		})
	})

	Context("replicas + suspend gate", func() {
		It("tracks spec.replicas 0->1->0 and never writes back to the CR spec", func() {
			name := newUser()
			inst := createInstance(name, ptr.To(int32(0)), false, "")
			child := config.ChildNamePrefix + name

			Eventually(func() (int32, error) {
				sts := &appsv1.StatefulSet{}
				if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: child}, sts); err != nil {
					return -1, err
				}
				return *sts.Spec.Replicas, nil
			}, timeout, interval).Should(Equal(int32(0)))

			By("activator patches replicas 0->1 under its own field manager")
			patchReplicas(name, 1)
			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(1)))

			By("back to 0")
			patchReplicas(name, 0)
			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(0)))

			By("the operator never wrote back to spec.replicas (still what the activator set)")
			Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(inst), inst)).To(Succeed())
			Expect(inst.Spec.Replicas).To(HaveValue(Equal(int32(0))))
		})

		It("suspended=true forces STS replicas to 0 even when replicas=1, and clears back to 1", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(1)), false, "")

			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(1)))

			By("suspend gates to 0")
			patchSuspended(name, true)
			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(0)))

			By("clearing suspend returns to replicas (1) without forcing a wake decision")
			patchSuspended(name, false)
			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(1)))
		})
	})

	Context("status", func() {
		It("reports phase + observedGeneration + conditions", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(0)), false, "")

			By("a suspended (replicas 0) instance settles to Suspended with Ready=True")
			Eventually(func(g Gomega) {
				inst := &ankiv1alpha1.AnkiInstance{}
				g.Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
				g.Expect(inst.Status.Phase).To(Equal(ankiv1alpha1.PhaseSuspended))
				g.Expect(inst.Status.ObservedGeneration).To(Equal(inst.Generation))
				g.Expect(inst.Status.CurrentImage).To(Equal(fleetImage))
				cond := findCondition(inst.Status.Conditions, ankiv1alpha1.ConditionReady)
				g.Expect(cond).NotTo(BeNil())
				g.Expect(cond.Status).To(Equal(metav1.ConditionTrue))
				g.Expect(cond.Reason).To(Equal("Suspended"))
			}, timeout, interval).Should(Succeed())
		})

		It("writes status under the SSA field manager anki-operator on the status subresource", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(0)), false, "")

			By("status is populated and its managedFields entry is owned by anki-operator/status")
			Eventually(func(g Gomega) {
				inst := &ankiv1alpha1.AnkiInstance{}
				g.Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
				g.Expect(inst.Status.Phase).NotTo(BeEmpty())

				var found bool
				for _, mf := range inst.ManagedFields {
					if mf.Manager == config.FieldOwner && mf.Subresource == "status" {
						found = true
					}
					// The operator must never own a spec field: no non-status entry
					// for anki-operator may exist.
					if mf.Manager == config.FieldOwner {
						g.Expect(mf.Subresource).To(Equal("status"),
							"anki-operator must only manage the status subresource, never spec")
					}
				}
				g.Expect(found).To(BeTrue(),
					"expected a managedFields entry manager=anki-operator subresource=status")
			}, timeout, interval).Should(Succeed())
		})
	})

	Context("validation (CEL, no webhook)", func() {
		It("rejects a spec.user that differs from metadata.name", func() {
			name := newUser()
			inst := &ankiv1alpha1.AnkiInstance{
				ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testNS},
				Spec:       ankiv1alpha1.AnkiInstanceSpec{User: "someone-else", Replicas: ptr.To(int32(0))},
			}
			Expect(k8sClient.Create(ctx, inst)).NotTo(Succeed())
		})

		It("rejects replicas=2", func() {
			name := newUser()
			inst := &ankiv1alpha1.AnkiInstance{
				ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testNS},
				Spec:       ankiv1alpha1.AnkiInstanceSpec{User: name, Replicas: ptr.To(int32(2))},
			}
			Expect(k8sClient.Create(ctx, inst)).NotTo(Succeed())
		})

		It("rejects mutating an immutable spec.user", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(0)), false, "")
			inst := &ankiv1alpha1.AnkiInstance{}
			Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
			inst.Spec.User = "changed"
			Expect(k8sClient.Update(ctx, inst)).NotTo(Succeed())
		})
	})

	Context("owner references for GC", func() {
		It("both children carry a blockOwnerDeletion controller ref back to the CR", func() {
			name := newUser()
			inst := createInstance(name, ptr.To(int32(0)), false, "")
			child := config.ChildNamePrefix + name

			Eventually(func() error {
				return k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: child}, &appsv1.StatefulSet{})
			}, timeout, interval).Should(Succeed())

			sts := getSTS(name)
			Expect(sts.OwnerReferences[0].UID).To(Equal(inst.UID))
			// Note: real cascading GC needs the real garbage collector, which envtest
			// does not run (requirements-operator §11.2). We assert the ref is present.
		})
	})

	Context("restartedAt (rollout-restart pattern, envtest case 8b)", func() {
		// Pod recreation semantics: the annotation lives on the POD TEMPLATE, so a
		// changed value changes the template hash and the StatefulSet controller
		// recreates the pod (kubectl rollout restart pattern). envtest runs no STS
		// controller, so the tests assert the template-level contract; the actual
		// pod churn is a real-cluster check (requirements-operator §11.2).

		It("renders no template annotation when the field was never set", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(1)), false, "")
			sts := eventuallySTS(name)
			Expect(sts.Spec.Template.Annotations).NotTo(HaveKey(config.AnnotationRestartedAt))
		})

		It("stamps the value verbatim, changes nothing else, no-ops when unchanged, and does not roll on clear", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(1)), false, "")
			before := eventuallySTS(name)
			Expect(before.Spec.Template.Annotations).NotTo(HaveKey(config.AnnotationRestartedAt))

			By("setting spec.restartedAt stamps the pod-template annotation verbatim")
			const nonce = "2026-07-11T12:00:00Z"
			patchRestartedAt(name, nonce)
			Eventually(func() (string, error) { return templateRestartedAt(name) },
				timeout, interval).Should(Equal(nonce))

			By("nothing else about the StatefulSet changed (PVC template, pod spec, replicas untouched)")
			after := getSTS(name)
			Expect(after.Spec.Template.Spec).To(Equal(before.Spec.Template.Spec))
			Expect(after.Spec.VolumeClaimTemplates).To(Equal(before.Spec.VolumeClaimTemplates))
			Expect(after.Spec.Replicas).To(Equal(before.Spec.Replicas))
			Expect(after.Spec.Template.Labels).To(Equal(before.Spec.Template.Labels))

			By("an unchanged value stays put across reconciles, with ZERO STS writes (an SSA no-op does not bump resourceVersion)")
			stampedRV := after.ResourceVersion
			Consistently(func(g Gomega) {
				got := getSTS(name)
				g.Expect(got.Spec.Template.Annotations[config.AnnotationRestartedAt]).To(Equal(nonce))
				g.Expect(got.ResourceVersion).To(Equal(stampedRV), "no-op reconciles must not write the STS")
			}, 2*time.Second, interval).Should(Succeed())

			By("changing the value re-stamps it (this is what rolls the pod)")
			const nonce2 = "2026-07-11T13:00:00Z"
			patchRestartedAt(name, nonce2)
			Eventually(func() (string, error) { return templateRestartedAt(name) },
				timeout, interval).Should(Equal(nonce2))

			By("clearing the field does NOT roll the pod: the last annotation is carried forward")
			patchRestartedAt(name, "")
			// Wait until the clear was observed (observedGeneration catches up) …
			Eventually(func(g Gomega) {
				inst := &ankiv1alpha1.AnkiInstance{}
				g.Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
				g.Expect(inst.Spec.RestartedAt).To(BeEmpty())
				g.Expect(inst.Status.ObservedGeneration).To(Equal(inst.Generation))
			}, timeout, interval).Should(Succeed())
			// … and the template hash input is unchanged: annotation still nonce2.
			Consistently(func() (string, error) { return templateRestartedAt(name) },
				2*time.Second, interval).Should(Equal(nonce2))
		})
	})

	Context("data fate on CR delete (finalizer, envtest case 8c)", func() {
		// envtest runs no garbage collector and no pvc-protection controller, so
		// these specs assert the operator's own contract: finalizer sequencing and
		// the PVC delete CALL (observed as a deletionTimestamp / NotFound). Actual
		// child GC + PVC removal are real-cluster checks (requirements-operator §11.2).

		It("defaults dataRetention to Retain and adds the finalizer", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(0)), false, "")

			inst := &ankiv1alpha1.AnkiInstance{}
			Eventually(func(g Gomega) {
				g.Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
				g.Expect(inst.Finalizers).To(ContainElement(config.Finalizer))
			}, timeout, interval).Should(Succeed())
			Expect(inst.Spec.DataRetention).To(Equal(ankiv1alpha1.DataRetentionRetain), "CRD default")
		})

		It("Retain (default): CR delete releases the finalizer and leaves the PVC untouched", func() {
			name := newUser()
			inst := createInstance(name, ptr.To(int32(0)), false, "")
			awaitFinalizer(name)
			pvc := createUserPVC(name)

			By("deleting the CR")
			Expect(k8sClient.Delete(ctx, inst)).To(Succeed())

			By("the finalizer is released and the CR goes away")
			Eventually(func() bool {
				return apierrors.IsNotFound(
					k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, &ankiv1alpha1.AnkiInstance{}))
			}, timeout, interval).Should(BeTrue())

			By("the PVC survives, not even marked for deletion")
			Consistently(func(g Gomega) {
				got := &corev1.PersistentVolumeClaim{}
				g.Expect(k8sClient.Get(ctx, client.ObjectKeyFromObject(pvc), got)).To(Succeed())
				g.Expect(got.DeletionTimestamp.IsZero()).To(BeTrue(), "PVC must not be deleted under Retain")
			}, 2*time.Second, interval).Should(Succeed())
		})

		It("Delete: CR delete drives the finalizer to delete the PVC, then releases", func() {
			name := newUser()
			inst := createInstance(name, ptr.To(int32(0)), false, "")
			awaitFinalizer(name)

			By("flipping dataRetention to Delete (lifecycle's delete saga)")
			Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
			inst.Spec.DataRetention = ankiv1alpha1.DataRetentionDelete
			Expect(k8sClient.Update(ctx, inst)).To(Succeed())

			pvc := createUserPVC(name)

			By("deleting the CR")
			Expect(k8sClient.Delete(ctx, inst)).To(Succeed())

			By("the operator issues the PVC delete (gone, or terminating if a protection finalizer holds it)")
			Eventually(func(g Gomega) {
				got := &corev1.PersistentVolumeClaim{}
				err := k8sClient.Get(ctx, client.ObjectKeyFromObject(pvc), got)
				if apierrors.IsNotFound(err) {
					return
				}
				g.Expect(err).NotTo(HaveOccurred())
				g.Expect(got.DeletionTimestamp.IsZero()).To(BeFalse(), "PVC must be deleted under dataRetention: Delete")
			}, timeout, interval).Should(Succeed())

			By("only then is the finalizer released and the CR goes away")
			Eventually(func() bool {
				return apierrors.IsNotFound(
					k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, &ankiv1alpha1.AnkiInstance{}))
			}, timeout, interval).Should(BeTrue())
		})

		It("Delete: an instance that never woke (no PVC) still deletes cleanly — STS removed first, finalizer released, no wedge", func() {
			name := newUser()
			inst := createInstance(name, ptr.To(int32(0)), false, "")
			awaitFinalizer(name)
			// Wait for the child STS so the finalizer's STS-first step has something
			// real to delete (deterministic ordering vs. the create reconcile).
			eventuallySTS(name)

			By("flipping dataRetention to Delete; deliberately creating NO PVC (the pod never ran)")
			Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
			inst.Spec.DataRetention = ankiv1alpha1.DataRetentionDelete
			Expect(k8sClient.Update(ctx, inst)).To(Succeed())

			By("deleting the CR")
			Expect(k8sClient.Delete(ctx, inst)).To(Succeed())

			By("the finalizer's STS-first step removes the child StatefulSet (envtest runs no ownerRef GC, so this delete is the operator's)")
			Eventually(func() bool {
				return apierrors.IsNotFound(
					k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: config.ChildNamePrefix + name}, &appsv1.StatefulSet{}))
			}, timeout, interval).Should(BeTrue())

			By("the missing PVC is tolerated (NotFound branch): the finalizer releases and the CR goes away")
			Eventually(func() bool {
				return apierrors.IsNotFound(
					k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, &ankiv1alpha1.AnkiInstance{}))
			}, timeout, interval).Should(BeTrue())
		})
	})

	Context("SSA does not clobber an activator replicas write", func() {
		It("keeps the activator's spec.replicas across an operator status reconcile", func() {
			name := newUser()
			createInstance(name, ptr.To(int32(0)), false, "")

			By("activator (a different field manager) sets replicas=1 via SSA")
			applyReplicasAsActivator(name, 1)

			By("the STS follows to 1 and, after the operator reconciles/writes status, spec.replicas is still 1")
			Eventually(func() (int32, error) { return stsReplicas(name) }, timeout, interval).Should(Equal(int32(1)))

			Consistently(func() (int32, error) {
				inst := &ankiv1alpha1.AnkiInstance{}
				if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst); err != nil {
					return -1, err
				}
				if inst.Spec.Replicas == nil {
					return -1, nil
				}
				return *inst.Spec.Replicas, nil
			}, 2*time.Second, interval).Should(Equal(int32(1)))
		})
	})
})

// --- helpers that model the other writers (activator/lifecycle) ---

func patchReplicas(name string, r int32) {
	inst := &ankiv1alpha1.AnkiInstance{}
	Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
	inst.Spec.Replicas = ptr.To(r)
	Expect(k8sClient.Update(ctx, inst)).To(Succeed())
}

func patchRestartedAt(name string, ts string) {
	inst := &ankiv1alpha1.AnkiInstance{}
	Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
	inst.Spec.RestartedAt = ts
	Expect(k8sClient.Update(ctx, inst)).To(Succeed())
}

// eventuallySTS waits for the child StatefulSet to exist and returns it.
func eventuallySTS(name string) *appsv1.StatefulSet {
	sts := &appsv1.StatefulSet{}
	Eventually(func() error {
		return k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: config.ChildNamePrefix + name}, sts)
	}, timeout, interval).Should(Succeed())
	return sts
}

// templateRestartedAt returns the restart-nonce annotation currently on the child
// StatefulSet's pod template ("" when absent); error while the STS does not exist,
// so it is safe to poll inside Eventually/Consistently.
func templateRestartedAt(name string) (string, error) {
	sts := &appsv1.StatefulSet{}
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: config.ChildNamePrefix + name}, sts); err != nil {
		return "", err
	}
	return sts.Spec.Template.Annotations[config.AnnotationRestartedAt], nil
}

// awaitFinalizer blocks until the operator has stamped its data-fate finalizer on
// the CR — deleting the CR before that point would bypass the finalizer flow the
// spec is exercising.
func awaitFinalizer(name string) {
	Eventually(func(g Gomega) {
		inst := &ankiv1alpha1.AnkiInstance{}
		g.Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
		g.Expect(inst.Finalizers).To(ContainElement(config.Finalizer))
	}, timeout, interval).Should(Succeed())
}

// createUserPVC simulates the PVC the StatefulSet controller would create from the
// volumeClaimTemplate (envtest runs no STS controller). The name is computed from
// the FETCHED child StatefulSet — <template>-<stsName>-<ordinal 0>, k8s's own
// derivation — NOT re-derived from the constants the production code uses, so drift
// between the rendered STS and the finalizer's pvcName() cannot pass silently.
func createUserPVC(name string) *corev1.PersistentVolumeClaim {
	sts := eventuallySTS(name)
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{
			Name:      sts.Spec.VolumeClaimTemplates[0].Name + "-" + sts.Name + "-0",
			Namespace: testNS,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOncePod},
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("1Gi")},
			},
		},
	}
	Expect(k8sClient.Create(ctx, pvc)).To(Succeed())
	return pvc
}

func patchSuspended(name string, s bool) {
	inst := &ankiv1alpha1.AnkiInstance{}
	Expect(k8sClient.Get(ctx, types.NamespacedName{Namespace: testNS, Name: name}, inst)).To(Succeed())
	inst.Spec.Suspended = s
	Expect(k8sClient.Update(ctx, inst)).To(Succeed())
}

// applyReplicasAsActivator writes ONLY spec.replicas using a distinct SSA field
// manager, exactly as the activator would (docs/contracts.md §2). It uses an
// unstructured patch so it claims ownership of nothing but spec.replicas.
func applyReplicasAsActivator(name string, r int32) {
	patch := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "anki.ankimcp.ai/v1alpha1",
			"kind":       "AnkiInstance",
			"metadata":   map[string]interface{}{"name": name, "namespace": testNS},
			"spec":       map[string]interface{}{"replicas": int64(r)},
		},
	}
	Expect(k8sClient.Patch(ctx, patch, client.Apply,
		client.FieldOwner("anki-activator"), client.ForceOwnership)).To(Succeed())
}

func findCondition(conds []metav1.Condition, t string) *metav1.Condition {
	for i := range conds {
		if conds[i].Type == t {
			return &conds[i]
		}
	}
	return nil
}
