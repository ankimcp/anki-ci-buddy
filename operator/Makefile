# AnkiInstance operator — Makefile
#
# Tooling is pinned to the kubebuilder line targeting k8s 1.36 / Go 1.26:
#   controller-runtime v0.24.x, controller-tools v0.21.x.
# controller-gen and setup-envtest are invoked via `go run` so no global install
# is required. Go's auto toolchain (GOTOOLCHAIN=auto) fetches Go 1.26 if needed.
#
# CI REQUIREMENT: run with GOTOOLCHAIN=auto (exported below) or provision Go >= 1.26.
# go.mod pins `go 1.26.0` (controller-runtime v0.24.1 floor); a runner on older Go
# with GOTOOLCHAIN=local fails the build. Do not override the export below in CI.

CONTROLLER_TOOLS_VERSION ?= v0.21.0
SETUP_ENVTEST_VERSION    ?= release-0.24
ENVTEST_K8S_VERSION      ?= 1.36.0

CONTROLLER_GEN = go run sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_TOOLS_VERSION)
SETUP_ENVTEST  = go run sigs.k8s.io/controller-runtime/tools/setup-envtest@$(SETUP_ENVTEST_VERSION)

IMG ?= anki-operator:latest

export GOTOOLCHAIN ?= auto

.PHONY: all
all: build

.PHONY: help
help: ## Display this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

##@ Development

# RBAC NOTE: controller-gen can only emit a ClusterRole, but the operator ships a
# hand-maintained NAMESPACED Role (config/rbac/role.yaml — least privilege, see its
# header). The generated rbac output therefore goes to the gitignored scratch dir
# config/rbac/generated/ so `make manifests` can NEVER overwrite the shipped file;
# generated/role.yaml exists only as a diff reference when the //+kubebuilder:rbac
# markers change — fold rule changes into config/rbac/role.yaml by hand, keeping
# kind: Role + metadata.namespace.
.PHONY: manifests
manifests: ## Generate CRD manifests (+ scratch RBAC reference in config/rbac/generated/).
	$(CONTROLLER_GEN) rbac:roleName=anki-operator-role crd \
		paths="./..." \
		output:crd:artifacts:config=config/crd/bases \
		output:rbac:artifacts:config=config/rbac/generated

.PHONY: generate
generate: ## Generate DeepCopy code.
	$(CONTROLLER_GEN) object:headerFile="hack/boilerplate.go.txt" paths="./..."

.PHONY: fmt
fmt: ## Run gofmt.
	gofmt -w .

.PHONY: vet
vet: ## Run go vet.
	go vet ./...

.PHONY: build
build: generate fmt vet ## Build the manager binary.
	go build -o bin/manager ./cmd

.PHONY: run
run: generate fmt vet ## Run the operator against the configured cluster.
	go run ./cmd --fleet-image=$(IMG)

##@ Test

ENVTEST_ASSETS = $(shell pwd)/bin

.PHONY: envtest-bin
envtest-bin: ## Download envtest control-plane binaries.
	$(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(ENVTEST_ASSETS) -p path

.PHONY: test
test: manifests generate fmt vet ## Run unit + envtest tests.
	KUBEBUILDER_ASSETS="$$($(SETUP_ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(ENVTEST_ASSETS) -p path)" \
		go test ./... -coverprofile cover.out

.PHONY: test-unit
test-unit: ## Run only the pure-function unit tests (no apiserver).
	go test ./internal/controller/ -run 'TestEffectiveReplicas|TestTargetImage|TestStsRolling|TestDecideImage|TestComputeStatusPhase' -v

##@ Build

.PHONY: docker-build
docker-build: ## Build the operator image.
	docker build -t $(IMG) .

##@ Deploy

.PHONY: install
install: manifests ## Install the CRD into the cluster.
	kubectl apply -f config/crd/bases

.PHONY: uninstall
uninstall: ## Remove the CRD from the cluster.
	kubectl delete -f config/crd/bases

.PHONY: deploy
deploy: manifests ## Deploy the operator via kustomize (Server-Side Apply).
	kubectl apply --server-side -k config/default

.PHONY: undeploy
undeploy: ## Remove the operator.
	kubectl delete -k config/default
