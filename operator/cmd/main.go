/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License").
*/

package main

import (
	"crypto/tls"
	"flag"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/controller"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(ankiv1alpha1.AddToScheme(scheme))
}

func main() {
	var (
		metricsAddr          string
		metricsSecure        bool
		probeAddr            string
		enableLeaderElection bool
		enableHTTP2          bool
	)

	cfg := config.Default()

	flag.StringVar(&metricsAddr, "metrics-bind-address", "0", "Address the metric endpoint binds to. Use :8443 for HTTPS or :8080 for HTTP, or 0 to disable.")
	flag.BoolVar(&metricsSecure, "metrics-secure", true, "Serve metrics over HTTPS with authn/authz.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081", "Address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false, "Enable leader election for controller manager, ensuring a single active reconciler.")
	flag.BoolVar(&enableHTTP2, "enable-http2", false, "Enable HTTP/2 for the metrics/webhook servers.")
	cfg.BindFlags(flag.CommandLine)

	opts := zap.Options{Development: false}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	if err := cfg.Validate(); err != nil {
		setupLog.Error(err, "invalid configuration")
		os.Exit(1)
	}
	if cfg.FleetImage == "" {
		setupLog.Info("WARNING: --fleet-image is empty; instances without spec.image will report Error until it is set")
	}

	// Disable HTTP/2 by default (Rapid Reset / mitigations) unless explicitly enabled.
	disableHTTP2 := func(c *tls.Config) { c.NextProtos = []string{"http/1.1"} }
	var tlsOpts []func(*tls.Config)
	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: metricsSecure,
		TLSOpts:       tlsOpts,
	}
	if metricsSecure {
		// Protect /metrics with the built-in authn/authz filter.
		metricsServerOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		WebhookServer:          webhook.NewServer(webhook.Options{TLSOpts: tlsOpts}),
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "anki-operator.ankimcp.ai",
		// Scope the cache/clients to the instances namespace (least privilege +
		// smaller cache for a large fleet).
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{cfg.Namespace: {}},
		},
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	reconciler := &controller.AnkiInstanceReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
		Cfg:    cfg,
	}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "AnkiInstance")
		os.Exit(1)
	}

	// Leader-scoped fleet-metrics sampler (requirements-operator §7 observability).
	if err := mgr.Add(&controller.FleetMetricsRunnable{
		Client:   mgr.GetClient(),
		Cfg:      cfg,
		Interval: time.Duration(cfg.MetricsIntervalSeconds) * time.Second,
	}); err != nil {
		setupLog.Error(err, "unable to add fleet-metrics runnable")
		os.Exit(1)
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager",
		"namespace", cfg.Namespace,
		"fleetImage", cfg.FleetImage,
		"storageClass", cfg.StorageClassName,
		"rolloutMaxConcurrent", cfg.RolloutMaxConcurrent)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}
