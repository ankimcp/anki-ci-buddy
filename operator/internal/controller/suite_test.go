package controller

import (
	"context"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	ankiv1alpha1 "github.com/ankimcp/anki-ci-buddy/operator/api/v1alpha1"
	"github.com/ankimcp/anki-ci-buddy/operator/internal/config"
)

var (
	cfg        *rest.Config
	k8sClient  client.Client
	testEnv    *envtest.Environment
	ctx        context.Context
	cancel     context.CancelFunc
	testNS     = "anki-instances"
	fleetImage = "repo/anki:fleet-1"
)

func TestControllers(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "AnkiInstance Controller Suite")
}

var _ = BeforeSuite(func() {
	logf.SetLogger(zap.New(zap.WriteTo(GinkgoWriter), zap.UseDevMode(true)))

	ctx, cancel = context.WithCancel(context.Background())

	By("bootstrapping test environment")
	testEnv = &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases")},
		ErrorIfCRDPathMissing: true,
		// setup-envtest places the control-plane binaries here; the Makefile exports
		// KUBEBUILDER_ASSETS, but resolve a versioned path as a fallback for `go test`.
		BinaryAssetsDirectory: filepath.Join("..", "..", "bin", "k8s",
			binaryAssetsSubdir()),
	}

	var err error
	cfg, err = testEnv.Start()
	Expect(err).NotTo(HaveOccurred())
	Expect(cfg).NotTo(BeNil())

	Expect(ankiv1alpha1.AddToScheme(scheme.Scheme)).To(Succeed())

	k8sClient, err = client.New(cfg, client.Options{Scheme: scheme.Scheme})
	Expect(err).NotTo(HaveOccurred())

	By("creating the instances namespace")
	Expect(k8sClient.Create(ctx, &corev1.Namespace{
		ObjectMeta: metav1.ObjectMeta{Name: testNS},
	})).To(Succeed())

	By("starting the manager + reconciler")
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:  scheme.Scheme,
		Metrics: metricsserver.Options{BindAddress: "0"},
	})
	Expect(err).NotTo(HaveOccurred())

	opCfg := config.Default()
	opCfg.Namespace = testNS
	opCfg.FleetImage = fleetImage
	opCfg.MaxConcurrentReconciles = 1

	reconciler := &AnkiInstanceReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
		Cfg:    opCfg,
	}
	Expect(reconciler.SetupWithManager(mgr)).To(Succeed())

	go func() {
		defer GinkgoRecover()
		Expect(mgr.Start(ctx)).To(Succeed())
	}()

	// Block until the manager's cache has synced before letting any spec run. The
	// manager starts asynchronously above; without this, the first specs race a cold
	// cache (client reads served from an unpopulated informer), which manifested as
	// intermittent Eventually timeouts. The manager here is not leader-elected, so a
	// synced cache is the correct readiness signal (use mgr.Elected() when it is).
	By("waiting for the manager cache to sync")
	Expect(mgr.GetCache().WaitForCacheSync(ctx)).To(BeTrue())
})

var _ = AfterSuite(func() {
	cancel()
	By("tearing down the test environment")
	// Give the manager a moment to stop before shutting down the apiserver.
	time.Sleep(100 * time.Millisecond)
	Expect(testEnv.Stop()).To(Succeed())
})

// binaryAssetsSubdir builds the version-specific directory setup-envtest creates,
// e.g. "1.36.0-darwin-arm64". The Makefile pins ENVTEST_K8S_VERSION.
func binaryAssetsSubdir() string {
	return "1.36.0-" + runtime.GOOS + "-" + runtime.GOARCH
}
