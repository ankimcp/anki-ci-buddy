package controller

import (
	"context"
	"time"

	"github.com/go-logr/logr"
	"github.com/prometheus/client_golang/prometheus"
	appsv1 "k8s.io/api/apps/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	"sigs.k8s.io/controller-runtime/pkg/metrics"

	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

// Fleet rollout gauges (requirements-operator §7 step 6). Registered with the
// controller-runtime metrics registry so they are exposed on the standard
// /metrics endpoint alongside workqueue/reconcile metrics.
var (
	fleetInstancesTotal = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "ankiinstance_fleet_instances_total",
		Help: "Total number of managed AnkiInstance StatefulSets.",
	})
	fleetInstancesOnTarget = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "ankiinstance_fleet_instances_on_target_image_total",
		Help: "StatefulSets whose anki container already carries the operator fleet image.",
	})
	fleetInstancesRolling = prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "ankiinstance_fleet_instances_rolling_total",
		Help: "StatefulSets mid-transition between pod-template revisions.",
	})
)

func init() {
	metrics.Registry.MustRegister(fleetInstancesTotal, fleetInstancesOnTarget, fleetInstancesRolling)
}

// FleetMetricsRunnable periodically resamples the fleet and updates the rollout
// gauges. It is leader-election-scoped (only the active operator samples) to avoid
// double-counting and to keep a single source of truth (requirements-operator §8).
type FleetMetricsRunnable struct {
	Client   client.Client
	Cfg      *config.Config
	Interval time.Duration
}

var _ manager.Runnable = &FleetMetricsRunnable{}
var _ manager.LeaderElectionRunnable = &FleetMetricsRunnable{}

// NeedLeaderElection makes this runnable start only on the elected leader.
func (m *FleetMetricsRunnable) NeedLeaderElection() bool { return true }

// Start runs the sampling loop until the context is cancelled.
func (m *FleetMetricsRunnable) Start(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("fleet-metrics")
	interval := m.Interval
	if interval <= 0 {
		interval = 30 * time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	m.sample(ctx, logger)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			m.sample(ctx, logger)
		}
	}
}

func (m *FleetMetricsRunnable) sample(ctx context.Context, logger logr.Logger) {
	var stsList appsv1.StatefulSetList
	if err := m.Client.List(ctx, &stsList,
		client.InNamespace(m.Cfg.Namespace),
		client.MatchingLabels{config.LabelManagedBy: config.LabelManagedByValue},
	); err != nil {
		logger.Error(err, "fleet metrics: list statefulsets failed")
		return
	}

	var total, onTarget, rolling float64
	for i := range stsList.Items {
		sts := &stsList.Items[i]
		total++
		if stsImage(sts) == m.Cfg.FleetImage && m.Cfg.FleetImage != "" {
			onTarget++
		}
		if stsRolling(sts) {
			rolling++
		}
	}
	fleetInstancesTotal.Set(total)
	fleetInstancesOnTarget.Set(onTarget)
	fleetInstancesRolling.Set(rolling)
}
