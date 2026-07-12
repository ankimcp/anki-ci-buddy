package controller

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// desiredService builds the headless Service child (requirements-operator §4.2,
// docs/contracts.md §7). clusterIP: None gives the StatefulSet stable pod DNS
// (anki-<id>-0.anki-<id>.<ns>.svc). The object is returned WITHOUT an ownerRef;
// the caller sets the controller ref before the Server-Side Apply.
//
// Ports: ONLY vnc-ws (6080) in v1 (DECIDED 2026-07-11, docs/contracts.md §7) —
// mcp 3141 stays a CONTAINER port but is not Service-exposed: nothing dials
// inbound MCP, it rides the add-on's outbound tunnel connection. Headless (not
// ClusterIP) is settled too: the VNC gateway dials the stable pod DNS by naming
// convention (requirements-operator §12 item 5, resolved).
func desiredService(instance *ankiv1alpha1.AnkiInstance, _ *config.Config) *corev1.Service {
	name := childName(instance)
	labels := commonLabels(instance)

	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "Service",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: instance.Namespace,
			Labels:    labels,
		},
		Spec: corev1.ServiceSpec{
			ClusterIP: corev1.ClusterIPNone,
			Selector:  labels,
			// PublishNotReadyAddresses so the VNC gateway can resolve the pod DNS name
			// while it is still coming up (it holds the request until Ready itself).
			PublishNotReadyAddresses: true,
			Ports: []corev1.ServicePort{
				{
					Name:       config.PortVNCWSName,
					Port:       config.PortVNCWS,
					TargetPort: intstr.FromString(config.PortVNCWSName),
					Protocol:   corev1.ProtocolTCP,
				},
			},
		},
	}
}
