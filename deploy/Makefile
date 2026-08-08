# =============================================================================
# base-services — one-command platform.
#
#   make setup    THE ONE COMMAND: provision -> bootstrap -> preflight -> rollout -> boot-heal
#   make up       bootstrap K3s (Calico + metrics-server + data stores)
#   make verify   PROVE the guarantees hold on this cluster
#   make deploy   install/upgrade all 16 services
#   make roll     health-gated rollout with per-service auto-rollback (server/update.sh)
#   make render   render everything locally (no cluster needed)
#   make test     the chart guards (no cluster needed)
#   make down     tear the namespace down
#
# `deploy` depends on `verify`: we do not install a platform whose deny-by-default
# is decorative or whose HPAs can never scale.
# =============================================================================
SHELL := /bin/bash
NAMESPACE ?= base-services
CHART := chart
VALUES := services
HELM ?= helm
KUBECTL ?= kubectl

SERVICES := $(notdir $(basename $(basename $(wildcard $(VALUES)/*.values.yaml))))

.PHONY: help up verify deploy setup roll reset render test down status $(addprefix deploy-,$(SERVICES))

help:
	@grep -E '^#   make' $(MAKEFILE_LIST) | sed 's/^#   /  /'

up:
	@bash k3s/bootstrap.sh

verify:
	@bash k3s/preflight.sh

render:
	@set -e; for f in $(VALUES)/*.values.yaml; do \
		n=$$(basename $$f .values.yaml); \
		echo "--- $$n"; \
		$(HELM) template $$n $(CHART) -f $$f > /dev/null; \
	done; echo "all $(words $(SERVICES)) services render"

test:
	@cd .. && python -m pytest tests/deploy -q

# Guard rail: `deploy` will not run on a cluster that cannot enforce the chart's promises.
deploy: verify
	@set -e; for f in $(VALUES)/*.values.yaml; do \
		n=$$(basename $$f .values.yaml); \
		echo "==> $$n"; \
		$(HELM) upgrade --install $$n $(CHART) -f $$f \
			--namespace $(NAMESPACE) --create-namespace --wait --timeout 5m; \
	done

deploy-%:
	$(HELM) upgrade --install $* $(CHART) -f $(VALUES)/$*.values.yaml \
		--namespace $(NAMESPACE) --create-namespace --wait --timeout 5m

# THE ONE COMMAND: provision -> bootstrap -> preflight -> (hooks) -> health-gated rollout -> boot-heal.
setup:
	@sudo NAMESPACE=$(NAMESPACE) bash server/setup.sh

# Safe rollout: render-first, per-service auto-rollback, --only, node-image prune. Prefer this over
# `deploy` on a live box. Pass flags through:  make roll ARGS="--only tenant --dry-run"
reset:
	@sudo NAMESPACE=$(NAMESPACE) bash server/reset-platform.sh $(ARGS)

roll:
	@sudo NAMESPACE=$(NAMESPACE) bash server/update.sh $(ARGS)

status:
	@$(KUBECTL) -n $(NAMESPACE) get deploy,hpa,pdb,netpol,cronjob

down:
	@$(KUBECTL) delete namespace $(NAMESPACE) --ignore-not-found
