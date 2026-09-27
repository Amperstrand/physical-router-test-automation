# ---------------------------------------------------------------------------
# Makefile — Top-level test runner for physical router test automation
#
# Wraps mint-health/Makefile and upstream-wifi/Makefile targets into a
# single entry point.  All variables (ROUTER, SSID, PASS, etc.) are
# forwarded to the sub-Makefiles.
#
# Quick reference:
#   make help                         # show all targets
#   make smoke-degraded ROUTER=alpha  # single-router degraded lifecycle (~3 min)
#   make smoke-upstream               # two-router degraded payment (~5 min)
#     make smoke-upstream-full SSID=MyNet PASS=secret ROUTER=alpha  # full upstream suite
#   make smoke-pin-upstream           # two-router pin-upstream test
#   make smoke-dynamic-rebuild ROUTER=alpha  # full→degraded→full lifecycle
#   make smoke-offline ROUTER=alpha   # block + restart + verify degraded
#   make smoke-recovery ROUTER=alpha  # unblock + wait for recovery
#   make test-startup-hygiene ROUTER=alpha  # boot with dead STA, verify switch
#   make test-startup-hygiene-dead-only ROUTER=alpha  # boot with ONLY dead STA
#   make test-captive-portal ROUTER=alpha  # Playwright captive portal tests
#   make test-cashu-payment ROUTER=alpha   # Playwright e2e cashu payment
#   make test-playwright              # Playwright LuCI admin UI tests
#   make test-all ROUTER=alpha        # everything (lint + playwright + smoke-degraded)
#   make deploy ROUTER=alpha          # cross-compile + deploy binaries
#   make status ROUTER=alpha          # check service status
#   make shell ROUTER=alpha           # interactive SSH session
#   make rescue-router ROUTER=beta VIA=alpha  # rescue offline router
#   make esp32-flash-a                # flash multi-mint firmware to Board A
#   make esp32-flash-b                # flash multi-mint firmware to Board B
#   make esp32-test-multi-mint-a      # full multi-mint test on Board A
#   make esp32-test-multi-mint-b      # full multi-mint test on Board B
#   make esp32-test-all-boards        # test both ESP32 boards
#
# Setup:
#   cp mint-health/routers.env.example mint-health/routers.env
#   cp upstream-wifi/routers.env.example upstream-wifi/routers.env
#   npm install && npx playwright install
# ---------------------------------------------------------------------------

ROUTER ?= alpha
SSID   ?=
PASS   ?=
MINT   ?= https://testnut-compat.mints.orangesync.tech
VIA    ?=
PHASE  ?=

BOLD   := \033[1m
GREEN  := \033[32m
RED    := \033[31m
YELLOW := \033[33m
CYAN   := \033[36m
RESET  := \033[0m

HARDWARE_LOCK := hardware.lock

include make/migration.mk

define require_hardware_lock
	@if [ ! -f "$(HARDWARE_LOCK)" ]; then \
		echo "$(RED)$(BOLD)Hardware not locked — run 'make lock PHASE=\"description\"' first$(RESET)"; \
		echo "$(YELLOW)Other LLM sessions may be using the hardware (ESP32 boards + routers).$(RESET)"; \
		exit 1; \
	fi
endef

# ===========================================================================
#  HELP
# ===========================================================================

.PHONY: help
help: ## Show this help
	@echo "$(BOLD)Physical Router Test Automation$(RESET)"
	@echo "$(BOLD)=====================================$(RESET)"
	@echo ""
	@echo "Usage:  make <target> ROUTER=alpha SSID=x PASS=y"
	@echo ""
	@echo "$(CYAN)--- Smoke tests (quick, 2-5 min) ---$(RESET)"
	@grep -E '^[a-z][a-z0-9_-]*:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-40s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)--- Full test suites (20+ min) ---$(RESET)"
	@echo "  make full-degraded         ROUTER=alpha    # full degraded lifecycle suite"
	@echo "  make full-upstream         SSID=x PASS=y   # full upstream WiFi suite"
	@echo "  make full-all              ROUTER=alpha     # all suites combined"
	@echo ""
	@echo "$(CYAN)--- Playwright tests ---$(RESET)"
	@echo "  make test-playwright                        # LuCI admin UI tests (requires .env)"
	@echo "  make test-captive-portal  ROUTER=alpha      # captive portal browser tests"
	@echo "  make test-cashu-payment   ROUTER=alpha      # e2e cashu payment via browser"
	@echo ""
	@echo "$(CYAN)--- Router management ---$(RESET)"
	@echo "  make hw-deploy              ROUTER=alpha      # cross-compile + deploy binaries"
	@echo "  make deploy-develop       ROUTER=alpha      # deploy from develop worktree"
	@echo "  make deploy-configwizzard ROUTER=alpha      # build + deploy configurationwizzard SPA"
	@echo "  make test-develop-smoke   ROUTER=alpha      # CLI config smoke tests"
	@echo "  make test-develop-playwright ROUTER=alpha    # Playwright tests vs develop"
	@echo "  make test-configwizzard-e2e ROUTER=alpha     # E2E: PR124 + configwizzard + :2121"
	@echo "  make test-configwizzard-all  ROUTER=alpha    # deploy-develop + deploy-configwizzard + full E2E"
	@echo "  make test-config-save     ROUTER=alpha      # config save round-trip + restart persistence"
	@echo "  make status               ROUTER=alpha      # check service status"
	@echo "  make shell                ROUTER=alpha      # interactive SSH session"
	@echo "  make logs                 ROUTER=alpha      # tail tollgate logs"
	@echo "  make check-sta-health     ROUTER=alpha      # verify no stale/duplicate STAs"
	@echo "  make fix-dns              ROUTER=alpha      # fix NetBird DNS hijack"
	@echo "  make cleanup              ROUTER=alpha      # remove blocks and restore config"
	@echo "  make rescue-router        ROUTER=beta VIA=alpha  # rescue offline router"
	@echo ""
	@echo "$(CYAN)--- Hostname & SSL tests ---$(RESET)"
	@echo "  make test-hostname        ROUTER=alpha      # verify hostname is set"
	@echo "  make test-ssl-full        ROUTER=alpha      # full SSL lifecycle test"
	@echo "  make test-ssl-self-signed ROUTER=alpha      # test self-signed SSL apply"
	@echo "  make test-ssl-remove      ROUTER=alpha      # test SSL remove"
	@echo "  make ssl-status           ROUTER=alpha      # show SSL status (read-only)"
	@echo "  make ssl-remove-force     ROUTER=alpha      # force-remove SSL config"
	@echo ""
	@echo "$(CYAN)--- Serial console (no-network) ---$(RESET)"
	@echo "  make serial-shell         ROUTER=alpha      # interactive serial console"
	@echo "  make serial-status        ROUTER=alpha      # status via serial"
	@echo "  make serial-cold-boot     ROUTER=alpha      # cold boot test with serial"
	@echo "  make serial-recovery      ROUTER=alpha CMD='wifi reload'  # emergency command"
	@echo ""
	@echo "$(CYAN)--- Hardware mutex (ESP32 + routers) ---$(RESET)"
	@echo "  make lock                 PHASE='testing foo'   # acquire lock"
	@echo "  make unlock                                      # release lock"
	@echo "  make lock-status                                 # check lock"
	@echo "  make force-unlock                                # force-release"
	@echo ""
	@echo "$(CYAN)--- Variables ---$(RESET)"
	@echo "  ROUTER  - router label from routers.env (default: alpha)"
	@echo "  SSID    - upstream WiFi SSID"
	@echo "  PASS    - upstream WiFi passphrase"
	@echo "  MINT    - mint URL for block/unblock (default: https://testnut-compat.mints.orangesync.tech)"
	@echo "  VIA     - intermediate router for rescue"
	@echo "  PHASE   - description for router lock"
	@echo ""
	@echo "$(CYAN)--- Arch component extraction tests (Board A, tollgate_core) ---$(RESET)"
	@echo "  make arch-build                   # build tollgate_core firmware"
	@echo "  make arch-flash-a                 # flash to Board A (requires lock)"
	@echo "  make arch-generate-spiffs         # generate SPIFFS with auto-detected WPA mode"
	@echo "  make arch-flash-spiffs-a          # flash SPIFFS to Board A (requires lock)"
	@echo "  make arch-bootlog-a               # capture boot log (requires lock)"
	@echo "  make arch-connect-a               # WiFi connect to Board A"
	@echo "  make arch-test-smoke              # smoke test (~30s)"
	@echo "  make arch-test-network            # network test (~15s)"
	@echo "  make arch-test-api                # API endpoint test (~20s)"
	@echo "  make arch-test-dns-fw             # DNS + firewall test (~30s)"
	@echo "  make arch-test-reset              # reset auth cycle (~30s)"
	@echo "  make arch-test-session            # session expiry (~80s)"
	@echo "  make arch-test-phase2             # full API test (~90s)"
	@echo "  make arch-test-full               # all tests (~4min)"
	@echo "  make arch-test-cleanup            # disconnect + reset auth"
	@echo ""
	@echo "$(CYAN)--- Local Relay tests (Board B, feature/local-relay) ---$(RESET)"
	@echo "  make relay-build                  # build relay firmware (no lock)"
	@echo "  make relay-flash-b                # flash to Board B (requires lock-b)"
	@echo "  make relay-test-smoke             # verify port 4869 reachable"
	@echo "  make relay-test-nip11             # NIP-11 info document test"
	@echo "  make relay-test-pubsub            # WS publish + subscribe test"
	@echo "  make relay-test-sync              # verify sync to public relays"
	@echo "  make relay-test-full              # all relay tests (~1min)"

# ===========================================================================
#  SMOKE TESTS
# ===========================================================================

.PHONY: smoke-degraded smoke-upstream smoke-upstream-full smoke-pin-upstream \
        smoke-dynamic-rebuild smoke-offline smoke-recovery \
        smoke-degraded-recovery smoke-degraded-connect fips-exit-smoke

smoke-degraded: ## Single-router degraded mode lifecycle (~3 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-degraded)

smoke-upstream: ## Two-router degraded upstream payment (~5 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-upstream)

smoke-upstream-full: ## Full upstream WiFi smoke test (requires SSID+PASS) [pytest]
	$(call require_hardware_lock)
	@if [ -z "$(SSID)" ]; then echo "$(RED)Error: SSID required. make smoke-upstream-full SSID=MyNet PASS=secret$(RESET)"; exit 1; fi
	$(call migrated_target,smoke-upstream-full)

smoke-pin-upstream: ## Two-router: verify upstream pin prevents scan-away [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-pin-upstream)

smoke-dynamic-rebuild: ## Full→degraded→full lifecycle [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-dynamic-rebuild)

smoke-offline: ## Block mint + restart + verify degraded (~2 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-offline)

smoke-recovery: ## Unblock mint + wait for recovery (~15 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-recovery)

smoke-degraded-recovery: ## Degraded→recovery without restart [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-degraded-recovery)

smoke-degraded-connect: ## WARNING: connect while degraded (RISKY) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,smoke-degraded-connect)

fips-exit-smoke: ## FIPS exit-node VPS smoke (WireGuard + nft MASQUERADE + nostr advert) [pytest]
	$(call migrated_target,fips-exit-smoke)

# ===========================================================================
#  STARTUP HYGIENE TESTS
# ===========================================================================

.PHONY: test-startup-hygiene test-startup-hygiene-dead-only

test-startup-hygiene: ## Boot with dead STA, verify auto-switch (~2 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-startup-hygiene)

test-startup-hygiene-dead-only: ## Boot with ONLY dead STA, emergency scan recovery (~3 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-startup-hygiene-dead-only)

# ===========================================================================
#  PLAYWRIGHT TESTS
# ===========================================================================

.PHONY: test-playwright test-captive-portal test-captive-portal-happy test-cashu-payment

test-playwright: ## Run Playwright LuCI admin UI tests (requires TOLLGATE_LUCI_PASSWORD)
	@if [ ! -d node_modules ]; then echo "$(YELLOW)Run npm install first$(RESET)"; exit 1; fi
	@cd tests && npx playwright test --config=playwright.config.mjs

test-captive-portal: ## Run Playwright captive portal tests against router [playwright]
	$(call require_hardware_lock)
	$(call migrated_target,test-captive-portal)

test-captive-portal-happy: ## Run only happy-path captive portal tests [playwright]
	$(call require_hardware_lock)
	$(call migrated_target,test-captive-portal-happy)

test-cashu-payment: ## Run cashu e2e payment Playwright test [playwright]
	$(call require_hardware_lock)
	$(call migrated_target,test-cashu-payment)

# ===========================================================================
#  BENCH LANES — read-only vs mutating (see docs/hw-lane-isolation.md)
# ===========================================================================

.PHONY: check-workflows hw-readonly

check-workflows: ## Guard: no PR-reachable workflow can reach the bench (+ self-test)
	@bash scripts/ci/check-workflow-hw-isolation.sh
	@bash scripts/ci/test-check-workflow-hw-isolation.sh

hw-readonly: ## Read-only bench surface check: no creds, no mutation, no paid traffic
	@# Deliberately takes NO hardware lock: it mutates nothing, so it must stay
	@# runnable while another agent holds the bench and while a session is live.
	@# Host via TOLLGATE_ROUTER_HOST (default 192.168.1.1).
	@bash scripts/hw-readonly-check.sh
#  DEMO RECORDING (docs/demo-recording.md)
# ===========================================================================

.PHONY: record-demo-clientd

TOLLGATE_LAB_COMPOSE ?= ../tollgate-module-basic-go/tests/cloud-lab/docker-compose.yml

record-demo-clientd: ## Record clientd auto-top-up demo vs cloud lab → evidence/ [demo]
	@test -f "$(TOLLGATE_LAB_COMPOSE)" || { echo "cloud-lab compose not found: $(TOLLGATE_LAB_COMPOSE) — set TOLLGATE_LAB_COMPOSE"; exit 1; }
	@mkdir -p /tmp/tollgate-demo-wallet
	docker compose -f $(TOLLGATE_LAB_COMPOSE) run --rm --entrypoint cdk-cli \
		-v /tmp/tollgate-demo-wallet:/w client -w /w mint http://mint:8085 100
	docker compose -f $(TOLLGATE_LAB_COMPOSE) restart upstream
	python3 scripts/record-demo.py \
		--clientd-cmd "docker compose -f $(TOLLGATE_LAB_COMPOSE) run --rm --entrypoint python3 \
			-v /tmp/tollgate-demo-wallet:/w -v $(CURDIR)/scripts:/prta:ro client \
			/prta/tollgate-clientd.py --gateway upstream --mac 02:00:00:00:00:20 \
			--wallet cdk-cli --wallet-dir /w --steps 1 --renew-below 45s --interval 1" \
		--log-source docker:tg-upstream \
		--duration 45 \
		--out evidence/$(shell date +%F)-clientd-demo \
		--title "PRTA laptop lane — clientd auto-top-up (cloud lab)" \
		--export-video --export-speed 2

.PHONY: test-laptop-clientd

# tests/laptop/ — clientd from this host against a real TollGate router
# (real ARP + NoDogSplash MAC registration). Requires provisioning, see
# docs/tollgate-clientd.md "The laptop lane": LAPTOP_GATEWAY (router IP),
# a funded cdk-cli wallet reachable in PATH, and SSH to the router.
test-laptop-clientd: ## [hardware] clientd vs real router: ARP + NDS MAC lane (tests/laptop/)
	$(call require_hardware_lock)
	@test -n "$${LAPTOP_GATEWAY:-}" || { echo "$(RED)Set LAPTOP_GATEWAY (e.g. 10.99.99.1)$(RESET)"; exit 1; }
	LAPTOP_GATEWAY="$${LAPTOP_GATEWAY}" $(PYTHON) -m pytest tests/laptop/ -v $(PYTEST_ARGS)

# ===========================================================================
#  FULL TEST SUITES
# ===========================================================================

.PHONY: full-degraded full-upstream full-all

full-degraded: ## Full mint health test suite (~20 min) [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,full-degraded)

full-upstream: ## Full upstream WiFi test suite (requires SSID+PASS, ~30 min)
	$(call require_hardware_lock)
	@if [ -z "$(SSID)" ]; then echo "$(RED)Error: SSID required. make full-upstream SSID=MyNet PASS=secret$(RESET)"; exit 1; fi
	@$(MAKE) -C mint-health r-full-upstream SSID="$(SSID)" PASS="$(PASS)" ROUTER=$(ROUTER)

full-all: ## Run all test suites (lint + playwright + degraded + upstream)
	$(call require_hardware_lock)
	@echo "$(BOLD)=======================================$(RESET)"
	@echo "$(BOLD)  Full Test Suite [$(ROUTER)]$(RESET)"
	@echo "$(BOLD)=======================================$(RESET)"
	@echo ""
	@echo "$(CYAN)1/3 — Playwright LuCI tests...$(RESET)"
	@-$(MAKE) test-playwright
	@echo ""
	@echo "$(CYAN)2/3 — Mint health degraded lifecycle...$(RESET)"
	@$(MAKE) full-degraded ROUTER=$(ROUTER) MINT="$(MINT)"
	@echo ""
	@echo "$(CYAN)3/3 — Captive portal + cashu payment...$(RESET)"
	@-$(MAKE) test-captive-portal ROUTER=$(ROUTER)
	@-$(MAKE) test-cashu-payment ROUTER=$(ROUTER)
	@echo ""
	@echo "$(BOLD)=======================================$(RESET)"
	@echo "$(GREEN)$(BOLD)  Full test suite complete [$(ROUTER)]$(RESET)"
	@echo "$(BOLD)=======================================$(RESET)"

# ===========================================================================
#  ROUTER MANAGEMENT
# ===========================================================================

.PHONY: hw-deploy deploy-ci deploy-cli status shell logs check-sta-health fix-dns cleanup \
        setup-fresh fund-wallet restore-prod diagnose-config test-default-mints \
        rescue-router save-upstream restore-upstream \
        test-hostname test-ssl-self-signed test-ssl-remove test-ssl-status test-ssl-full \
        ssl-status ssl-remove-force \
        test-ssl-setup-verify test-ssl-self-signed-yes test-ssl-reapply \
        test-ssl-remove-no-backup test-ssl-verify-cert test-ssl-verify-nds \
        test-ssl-verify-no-dns test-ssl-idempotent \
        test-ssl-comprehensive \
        test-ssl-real-cert test-ssl-real-cert-remove test-ssl-real-cert-full \
        test-ssl-all \
        deploy-develop test-develop-smoke test-develop-smoke-persist test-develop-playwright \
        deploy-configwizzard test-configwizzard-e2e test-configwizzard-all \
        test-config-save

DEVELOP_SRC ?= $(HOME)/tollgate-worktrees/develop/src
DEVICE ?= gl-mt3000
RESTART_WAIT ?= 10

hw-deploy: ## Cross-compile and deploy daemon + CLI to router
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-deploy ROUTER=$(ROUTER)

deploy-cli: ## Cross-compile and deploy CLI only (no service restart)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-deploy-cli ROUTER=$(ROUTER)

deploy-develop: ## Cross-compile from develop worktree and deploy daemon + CLI to router
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== Deploying develop branch to $(ROUTER) ($$router_host) [$(DEVICE)] ===$(RESET)"; \
	if [ ! -d "$(DEVELOP_SRC)" ]; then echo "$(RED)Develop worktree not found at $(DEVELOP_SRC)$(RESET)"; exit 1; fi; \
	echo "$(CYAN)1/4 — Building tollgate-wrt...$(RESET)"; \
	if [ "$(DEVICE)" = "gl-ar300" ]; then \
		cd "$(DEVELOP_SRC)" && GOOS=linux GOARCH=mips GOMIPS=softfloat CGO_ENABLED=0 go build -o /tmp/tollgate-wrt-develop -trimpath -ldflags="-s -w"; \
	else \
		cd "$(DEVELOP_SRC)" && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o /tmp/tollgate-wrt-develop -trimpath -ldflags="-s -w"; \
	fi; \
	echo "$(CYAN)2/4 — Building tollgate CLI...$(RESET)"; \
	if [ "$(DEVICE)" = "gl-ar300" ]; then \
		cd "$(DEVELOP_SRC)/cmd/tollgate-cli" && GOOS=linux GOARCH=mips GOMIPS=softfloat CGO_ENABLED=0 go build -o /tmp/tollgate-cli-develop -trimpath -ldflags="-s -w"; \
	else \
		cd "$(DEVELOP_SRC)/cmd/tollgate-cli" && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o /tmp/tollgate-cli-develop -trimpath -ldflags="-s -w"; \
	fi; \
	echo "$(CYAN)3/4 — Deploying to router...$(RESET)"; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "/etc/init.d/tollgate-wrt stop" 2>/dev/null || true; \
	scp -O -o ConnectTimeout=10 -o StrictHostKeyChecking=no /tmp/tollgate-wrt-develop root@$$router_host:/usr/bin/tollgate-wrt; \
	scp -O -o ConnectTimeout=10 -o StrictHostKeyChecking=no /tmp/tollgate-cli-develop root@$$router_host:/usr/bin/tollgate; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "chmod +x /usr/bin/tollgate-wrt /usr/bin/tollgate && /etc/init.d/tollgate-wrt start"; \
	echo "$(CYAN)4/4 — Verifying...$(RESET)"; \
	sleep 3; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "tollgate --json config schema" > /dev/null 2>&1 && echo "$(GREEN)Deploy OK — config schema responds$(RESET)" || echo "$(RED)Deploy FAILED — config schema not responding$(RESET)"

test-develop-smoke: ## Run CLI config smoke tests on router with develop binaries
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	SH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host"; \
	PASS=0; FAIL=0; \
	echo "$(BOLD)=== Develop Smoke Tests [$(ROUTER)] ===$(RESET)"; \
	echo ""; \
	echo "$(CYAN)--- config schema ---$(RESET)"; \
	$$SH "tollgate --json config schema" > /tmp/smoke-schema.json 2>&1 && grep -q '"json_key"' /tmp/smoke-schema.json && { echo "  $(GREEN)PASS$(RESET): schema returns FieldSchema array"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): schema not responding"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- config get ---$(RESET)"; \
	$$SH "tollgate --json config get" > /tmp/smoke-config.json 2>&1 && grep -q '"metric"' /tmp/smoke-config.json && { echo "  $(GREEN)PASS$(RESET): config get returns full config"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): config get failed"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- config set + disk persistence ---$(RESET)"; \
	ORIG_LOGLEVEL=$$($$SH "tollgate --json config get" 2>&1 | grep -oP '"log_level"\s*:\s*"\K[^"]*' | head -1); \
	SET_OUT=$$($$SH "tollgate --json config set log_level warn" 2>&1); \
	echo "$$SET_OUT" | grep -qP '"value"\s*:\s*"warn"' && { echo "  $(GREEN)PASS$(RESET): config set returns new value"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): config set failed"; FAIL=$$((FAIL+1)); }; \
	DISK_LEVEL=$$($$SH "grep log_level /etc/tollgate/config.json" 2>&1 | grep -oP '"log_level"\s*:\s*"\K[^"]*'); \
	if [ "$$DISK_LEVEL" = "warn" ]; then echo "  $(GREEN)PASS$(RESET): value persisted to /etc/tollgate/config.json"; PASS=$$((PASS+1)); else echo "  $(RED)FAIL$(RESET): disk has $$DISK_LEVEL (expected warn)"; FAIL=$$((FAIL+1)); fi; \
	$$SH "tollgate --json config set log_level $$ORIG_LOGLEVEL" > /dev/null 2>&1; \
	echo "$(CYAN)--- enum validation ---$(RESET)"; \
	ERR=$$($$SH "tollgate --json config set log_level INVALID" 2>&1); \
	echo "$$ERR" | grep -qi "not in allowed" && { echo "  $(GREEN)PASS$(RESET): rejects invalid log_level enum"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): accepted invalid enum (got: $$ERR)"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- min/max validation (upper bound) ---$(RESET)"; \
	ERR=$$($$SH "tollgate --json config set margin 5.0" 2>&1); \
	echo "$$ERR" | grep -qi "exceeds maximum" && { echo "  $(GREEN)PASS$(RESET): rejects margin > 1.0"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): accepted margin 5.0 (got: $$ERR)"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- min/max validation (lower bound) ---$(RESET)"; \
	ERR=$$($$SH "tollgate --json config set margin -- -0.5" 2>&1); \
	echo "$$ERR" | grep -qi "below minimum" && { echo "  $(GREEN)PASS$(RESET): rejects negative margin"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): accepted margin -0.5 (got: $$ERR)"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- wallet balance ---$(RESET)"; \
	$$SH "tollgate --json wallet balance" > /tmp/smoke-wallet.json 2>&1 && grep -q '"balance_sats"' /tmp/smoke-wallet.json && { echo "  $(GREEN)PASS$(RESET): wallet balance responds"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): wallet balance failed"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- health ---$(RESET)"; \
	$$SH "tollgate --json health" > /dev/null 2>&1 && { echo "  $(GREEN)PASS$(RESET): health responds"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): health failed"; FAIL=$$((FAIL+1)); }; \
	echo "$(CYAN)--- status ---$(RESET)"; \
	$$SH "tollgate --json status" > /dev/null 2>&1 && { echo "  $(GREEN)PASS$(RESET): status responds"; PASS=$$((PASS+1)); } || { echo "  $(RED)FAIL$(RESET): status failed"; FAIL=$$((FAIL+1)); }; \
	echo ""; \
	echo "$(BOLD)=== Results: $(GREEN)$$PASS passed$(RESET), $(RED)$$FAIL failed$(RESET) ==="; \
	if [ "$$FAIL" -gt 0 ]; then exit 1; fi

test-develop-smoke-persist: ## Run set+restart persistence test
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== Persistence Test [$(ROUTER)] ===$(RESET)"; \
	ORIG=$$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "tollgate --json config get" 2>&1 | grep -oP '"log_level"\s*:\s*"\K[^"]*' | head -1); \
	echo "  Original log_level: $$ORIG"; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "tollgate --json config set log_level error" > /dev/null 2>&1; \
	echo "  Restarting service..."; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "/etc/init.d/tollgate-wrt restart" 2>&1 || true; \
	echo "  Waiting $(RESTART_WAIT)s for service startup..."; \
	sleep $(RESTART_WAIT); \
	NEW=$$(ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "tollgate --json config get" 2>&1 | grep -oP '"log_level"\s*:\s*"\K[^"]*' | head -1); \
	echo "  After restart log_level: $$NEW"; \
	if [ "$$NEW" = "error" ]; then echo "  $(GREEN)PASS$(RESET): log_level persisted after restart"; \
	else echo "  $(RED)FAIL$(RESET): log_level not persisted (got $$NEW)"; fi; \
	ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@$$router_host "tollgate --json config set log_level $$ORIG" > /dev/null 2>&1

test-develop-playwright: ## Run Playwright tests against router with develop binaries
	$(call require_hardware_lock)
	@if [ ! -d node_modules ]; then echo "$(YELLOW)Run npm install first$(RESET)"; exit 1; fi
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== Playwright Tests [$(ROUTER)] ($$router_host) ===$(RESET)"; \
	TOLLGATE_SSH_HOST=$$router_host \
	TOLLGATE_CAPTIVE_PORTAL_HOST=$$router_host \
	npx playwright test tests/tollgate.spec.mjs tests/captive-portal.spec.mjs --project=desktop

CONFIGWIZZARD_REPO ?= /tmp/configurationwizzard

deploy-configwizzard: ## Build and deploy configurationwizzard SPA (admin + portal + rpcd plugin)
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	if [ ! -d "$(CONFIGWIZZARD_REPO)" ]; then echo "$(RED)configurationwizzard repo not found at $(CONFIGWIZZARD_REPO)$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== Deploying configurationwizzard to $(ROUTER) ($$router_host) ===$(RESET)"; \
	bash scripts/deploy-configwizzard.sh "$$router_host" "$(CONFIGWIZZARD_REPO)"

test-configwizzard-e2e: ## Run E2E tests: PR124 CLI + rpcd plugin + :2121 API + SPA integration
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== configurationwizzard E2E Tests [$(ROUTER)] ($$router_host) ===$(RESET)"; \
	bash scripts/test-configwizzard-e2e.sh "$$router_host"

test-configwizzard-all: ## Deploy everything + run full E2E test suite
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=======================================$(RESET)"; \
	echo "$(BOLD)  Full Configwizzard E2E [$(ROUTER)]$(RESET)"; \
	echo "$(BOLD)=======================================$(RESET)"; \
	echo ""; \
	echo "$(CYAN)1/3 — Deploying develop branch...$(RESET)"; \
	$(MAKE) deploy-develop ROUTER=$(ROUTER); \
	echo ""; \
	echo "$(CYAN)2/3 — Deploying configurationwizzard SPA...$(RESET)"; \
	$(MAKE) deploy-configwizzard ROUTER=$(ROUTER) CONFIGWIZZARD_REPO=$(CONFIGWIZZARD_REPO); \
	echo ""; \
	echo "$(CYAN)3/3 — Running E2E tests...$(RESET)"; \
	$(MAKE) test-configwizzard-e2e ROUTER=$(ROUTER); \
	echo ""; \
	echo "$(BOLD)=======================================$(RESET)"; \
	echo "$(GREEN)$(BOLD)  Full Configwizzard E2E complete [$(ROUTER)]$(RESET)"; \
	echo "$(BOLD)=======================================$(RESET)"

test-config-save: ## Run config save round-trip tests (save + disk verify + restart persistence)
	$(call require_hardware_lock)
	@router_host=$$(grep -E "^ROUTER_$$(echo $(ROUTER) | tr '[:lower:]' '[:upper:]')_HOST=" mint-health/routers.env | cut -d= -f2); \
	if [ -z "$$router_host" ]; then echo "$(RED)Unknown router '$(ROUTER)'$(RESET)"; exit 1; fi; \
	echo "$(BOLD)=== Config Save E2E Tests [$(ROUTER)] ($$router_host) ===$(RESET)"; \
	bash scripts/test-config-save-e2e.sh "$$router_host"

status: ## Check tollgate service status
	@$(MAKE) -C mint-health r-status ROUTER=$(ROUTER)

shell: ## Interactive SSH session on router
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-shell ROUTER=$(ROUTER)

logs: ## Tail tollgate logs (Ctrl+C to stop)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-logs ROUTER=$(ROUTER)

check-sta-health: ## Verify no stale/duplicate STA sections
	@$(MAKE) -C mint-health r-check-sta-health ROUTER=$(ROUTER)

fix-dns: ## Fix NetBird DNS hijack on router
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-fix-dns ROUTER=$(ROUTER)

cleanup: ## Remove mint blocks and restore config
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-cleanup ROUTER=$(ROUTER)

setup-fresh: ## Setup freshly flashed router (deploy + config + restart)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-setup-fresh ROUTER=$(ROUTER) MINT="$(MINT)"

fund-wallet: ## Fund wallet with 1013 sats from test mint
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-fund-wallet ROUTER=$(ROUTER)

restore-prod: ## Restore production config and restart
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-restore-prod-config ROUTER=$(ROUTER) MINT="$(MINT)"

diagnose-config: ## Verify service reads config from /etc/tollgate
	@$(MAKE) -C mint-health r-diagnose-config-path ROUTER=$(ROUTER)

test-default-mints: ## Verify default mint config
	@$(MAKE) -C mint-health r-test-default-mints ROUTER=$(ROUTER)

rescue-router: ## Rescue offline router via another router (ROUTER=beta VIA=alpha)
	$(call require_hardware_lock)
	@if [ -z "$(VIA)" ]; then echo "$(RED)Error: VIA required. make rescue-router ROUTER=beta VIA=alpha$(RESET)"; exit 1; fi
	@$(MAKE) -C mint-health r-rescue-router ROUTER=$(ROUTER) VIA=$(VIA)

save-upstream: ## Save current upstream SSID for later restore
	@$(MAKE) -C mint-health r-save-upstream ROUTER=$(ROUTER)

restore-upstream: ## Restore previously saved upstream SSID
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-restore-upstream ROUTER=$(ROUTER)

# ===========================================================================
#  MINT BLOCKING (for manual degraded mode testing)
# ===========================================================================

.PHONY: block-mint unblock-mint restart-service check-merchant check-degraded

block-mint: ## Block mint via /etc/hosts override
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-block-mint ROUTER=$(ROUTER) MINT="$(MINT)"

unblock-mint: ## Remove mint /etc/hosts override
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-unblock-mint ROUTER=$(ROUTER) MINT="$(MINT)"

restart-service: ## Restart tollgate service
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-restart-service ROUTER=$(ROUTER)

check-merchant: ## Verify full merchant mode (online)
	@$(MAKE) -C mint-health r-check-merchant ROUTER=$(ROUTER)

check-degraded: ## Verify degraded merchant mode (offline)
	@$(MAKE) -C mint-health r-check-degraded ROUTER=$(ROUTER)

# ===========================================================================
#  SERIAL CONSOLE
# ===========================================================================

.PHONY: serial-shell serial-status serial-logs serial-cold-boot serial-recovery \
        serial-cleanup serial-watch serial-boot-log

serial-shell: ## Interactive serial console (picocom)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health s-shell ROUTER=$(ROUTER)

serial-status: ## Check tollgate status via serial
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health s-status ROUTER=$(ROUTER)

serial-watch: ## Watch serial output (Ctrl+C to stop)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health s-watch ROUTER=$(ROUTER)

serial-cold-boot: ## Full cold boot test with serial monitoring [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,serial-cold-boot)

serial-boot-log: ## Capture full boot output
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health s-boot-log ROUTER=$(ROUTER)

serial-recovery: ## Emergency recovery via serial (CMD='...') [pymake]
	$(call require_hardware_lock)
	@if [ -z "$(CMD)" ]; then echo "$(RED)Error: CMD required. make serial-recovery ROUTER=alpha CMD='wifi reload'$(RESET)"; exit 1; fi
	$(call migrated_target,serial-recovery)

serial-cleanup: ## Cleanup mint blocks via serial
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health s-cleanup ROUTER=$(ROUTER)

# ===========================================================================
#  HYBRID (SSH first, serial fallback)
# ===========================================================================

.PHONY: hybrid-status hybrid-cleanup hybrid-restart-and-watch

hybrid-status: ## Status check — SSH first, serial fallback
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health h-status ROUTER=$(ROUTER)

hybrid-cleanup: ## Cleanup — SSH first, serial fallback
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health h-cleanup ROUTER=$(ROUTER)

hybrid-restart-and-watch: ## Restart service via SSH, verify via serial if SSH drops
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health h-restart-and-watch ROUTER=$(ROUTER)

# ===========================================================================
#  HARDWARE MUTEX (unified — protects OpenWRT routers; ESP32 has per-board)
# ===========================================================================

.PHONY: lock unlock lock-status force-unlock

LOCK_DIR := locks

lock: ## Acquire router hardware lock — set PHASE='description'
	@if [ -f "$(HARDWARE_LOCK)" ]; then \
		owner=$$(grep '^session:' $(HARDWARE_LOCK) | head -1 | sed 's/session: *//' | cut -d@ -f1); \
		if [ "$$owner" = "$$USER" ]; then \
			echo "$(YELLOW)Hardware already locked by this session — refreshing lock$(RESET)"; \
		else \
			echo "$(RED)$(BOLD)Cannot acquire lock — hardware locked by another session:$(RESET)"; \
			echo ""; \
			cat $(HARDWARE_LOCK); \
			echo ""; \
			echo "$(YELLOW)Use 'make force-unlock' to override (with caution).$(RESET)"; \
			exit 1; \
		fi; \
	fi; \
	branch=$$(git branch --show-current 2>/dev/null || echo "unknown"); \
	worktree=$$(pwd); \
	echo "locked: true" > $(HARDWARE_LOCK); \
	echo "branch: $$branch" >> $(HARDWARE_LOCK); \
	echo "worktree: $$worktree" >> $(HARDWARE_LOCK); \
	echo "session: $$USER@$$(hostname)" >> $(HARDWARE_LOCK); \
	echo "timestamp: $$(date -u '+%Y-%m-%dT%H:%M:%SZ')" >> $(HARDWARE_LOCK); \
	echo "phase: $(PHASE)" >> $(HARDWARE_LOCK); \
	echo "$(GREEN)$(BOLD)Router hardware lock acquired$(RESET)"; \
	cat $(HARDWARE_LOCK)

unlock: ## Release router hardware lock
	@if [ ! -f "$(HARDWARE_LOCK)" ]; then \
		echo "$(YELLOW)No lock file found — already unlocked.$(RESET)"; \
		exit 0; \
	fi; \
	echo "$(YELLOW)Releasing hardware lock...$(RESET)"; \
	rm -f $(HARDWARE_LOCK); \
	echo "$(GREEN)Hardware lock released.$(RESET)"

lock-status: ## Show all lock statuses (routers + ESP32 boards)
	@echo "$(BOLD)=== Router Lock ===$(RESET)"
	@if [ ! -f "$(HARDWARE_LOCK)" ]; then \
		echo "$(GREEN)Router hardware unlocked — available.$(RESET)"; \
	else \
		echo "$(YELLOW)$(BOLD)Router hardware locked:$(RESET)"; \
		echo ""; \
		cat $(HARDWARE_LOCK); \
	fi
	@echo ""
	@echo "$(BOLD)=== ESP32 Board Locks ===$(RESET)"
	@$(MAKE) -C esp32 lock-status

force-unlock: ## Force-release router hardware lock (use with caution)
	@if [ ! -f "$(HARDWARE_LOCK)" ]; then \
		echo "$(YELLOW)No lock file found — already unlocked.$(RESET)"; \
		exit 0; \
	fi; \
	echo "$(RED)$(BOLD)WARNING: Force-releasing router hardware lock!$(RESET)"; \
	echo "$(RED)Previous holder:$(RESET)"; \
	cat $(HARDWARE_LOCK); \
	rm -f $(HARDWARE_LOCK); \
	echo "$(GREEN)Hardware lock force-released.$(RESET)"

# ===========================================================================
#  HOSTNAME & SSL TESTS (legacy Makefile reference)
#
#  Routine PR validation for the Go SSL CLI has moved to pytest:
#    tests/api/test_ssl_go_cli.py  (PR #123-gated)
#  These targets remain available as manual/reference hardware commands.
# ===========================================================================

test-hostname: ## Verify hostname setup on router [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-hostname)

test-ssl-self-signed: ## Test self-signed SSL apply on router [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-self-signed)

test-ssl-remove: ## Test SSL remove on router [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-remove)

test-ssl-status: ## Test tollgate ssl status command on router [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-status)

test-ssl-full: ## Full SSL lifecycle [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-full)

ssl-status: ## Show current SSL status on router (read-only, no lock)
	@$(MAKE) -C mint-health r-ssl-status ROUTER=$(ROUTER)

ssl-remove-force: ## Force-remove SSL config on router (cleanup)
	$(call require_hardware_lock)
	@$(MAKE) -C mint-health r-ssl-remove-force ROUTER=$(ROUTER)

test-ssl-setup-verify: ## Verify router is in clean SSL state [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-setup-verify)

test-ssl-self-signed-yes: ## Test self-signed apply with --yes flag [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-self-signed-yes)

test-ssl-reapply: ## Test re-apply with existing backup [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-reapply)

test-ssl-remove-no-backup: ## Test remove when no backup exists [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-remove-no-backup)

test-ssl-verify-cert: ## Deep cert validation [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-verify-cert)

test-ssl-verify-nds: ## Verify nodogsplash allows port 443 [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-verify-nds)

test-ssl-verify-no-dns: ## Verify no dnsmasq domain for self-signed [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-verify-no-dns)

test-ssl-idempotent: ## Test apply twice [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-idempotent)

test-ssl-comprehensive: ## All self-signed SSL tests [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-comprehensive)

test-ssl-real-cert: ## Real cert via LE staging + Cloudflare [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-real-cert)

test-ssl-real-cert-remove: ## Real cert removal [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-real-cert-remove)

test-ssl-real-cert-full: ## Full real cert lifecycle [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-real-cert-full)

test-ssl-all: ## ALL SSL tests [pytest]
	$(call require_hardware_lock)
	$(call migrated_target,test-ssl-all)

# ===========================================================================
#  SETUP
# ===========================================================================

.PHONY: setup

setup: ## Install dependencies (npm + playwright + serial)
	@echo "$(BOLD)=== Installing dependencies ===$(RESET)"
	@echo "$(CYAN)1/3 — npm install...$(RESET)"
	@npm install
	@echo "$(CYAN)2/3 — Playwright browsers...$(RESET)"
	@npx playwright install
	@echo "$(CYAN)3/3 — Serial dependencies...$(RESET)"
	@pip3 install -q -r scripts/requirements-serial.txt 2>/dev/null || echo "$(YELLOW)pip install skipped (optional for serial tests)$(RESET)"
	@echo ""
	@echo "$(GREEN)Dependencies installed.$(RESET)"
	@echo ""
	@echo "$(YELLOW)Next steps:$(RESET)"
	@echo "  cp mint-health/routers.env.example mint-health/routers.env   # fill in real values"
	@echo "  cp upstream-wifi/routers.env.example upstream-wifi/routers.env"
	@echo "  cp .env.example .env                                         # fill in LuCI credentials"
	@echo ""
	@echo "$(CYAN)--- ESP32 board provisioning ---$(RESET)"
	@echo "  make esp32-provision-a                   # full provision Board A (erase + fw + SPIFFS + wait)"
	@echo "  make esp32-provision-b                   # full provision Board B"
	@echo "  make esp32-provision-c                   # full provision Board C"
	@echo "  make esp32-flash-a                       # flash firmware only to Board A"
	@echo "  make esp32-flash-b                       # flash firmware only to Board B"
	@echo "  make esp32-flash-c                       # flash firmware only to Board C"
	@echo "  make esp32-reset-a                       # reset Board A (no reflash)"
	@echo "  make esp32-reset-b                       # reset Board B"
	@echo "  make esp32-reset-c                       # reset Board C"
	@echo "  make esp32-wait-ready-a                  # poll Board A :2121 until ready"
	@echo "  make esp32-wait-ready-b                  # poll Board B :2121 until ready"
	@echo "  make esp32-wait-ready-c                  # poll Board C :2121 until ready"
	@echo ""
	@echo "$(CYAN)--- ESP32 board tests ---$(RESET)"
	@echo "  make esp32-test-multi-mint-a             # full multi-mint test on Board A"
	@echo "  make esp32-test-multi-mint-b             # full multi-mint test on Board B"
	@echo "  make esp32-test-all-boards               # test both ESP32 boards"
	@echo ""
	@echo "$(CYAN)--- ESP32 ContextVM tests ---$(RESET)"
	@echo "  make esp32-test-cvm-a                    # CVM announcement test on Board A"
	@echo "  make esp32-test-cvm-b                    # CVM announcement test on Board B"
	@echo "  make esp32-test-cvm-mcp-a                # MCP tools/call end-to-end on Board A"
	@echo "  make esp32-cvm-pubkey-a                  # print Board A CVM npub"
	@echo "  make esp32-cvm-pubkey-b                  # print Board B CVM npub"
	@echo ""
	@echo "$(CYAN)--- ESP32 Local Relay tests ---$(RESET)"
	@echo "  make relay-build                         # build relay firmware (no lock)"
	@echo "  make relay-flash-b                       # flash relay firmware to Board B"
	@echo "  make relay-test-smoke                    # verify relay port 4869"
	@echo "  make relay-test-nip11                    # NIP-11 relay info test"
	@echo "  make relay-test-pubsub                   # WS pub/sub test"
	@echo "  make relay-test-full                     # all relay tests"

# ===========================================================================
#  ESP32 BOARD TESTS (per-board locks)
# ===========================================================================

 .PHONY: esp32-flash-a esp32-flash-b esp32-flash-c \
         esp32-monitor-a esp32-monitor-b esp32-monitor-c \
         esp32-connect-a esp32-connect-b esp32-disconnect \
         esp32-reset-a esp32-reset-b esp32-reset-c \
         esp32-wait-ready-a esp32-wait-ready-b esp32-wait-ready-c \
         esp32-provision-a esp32-provision-b esp32-provision-c \
         esp32-test-discovery-a esp32-test-discovery-b \
         esp32-test-mints-a esp32-test-mints-b \
         esp32-test-multi-mint-a esp32-test-multi-mint-b esp32-test-all-boards \
         esp32-test-cvm-a esp32-test-cvm-b esp32-test-cvm-mcp-a \
         esp32-cvm-pubkey-a esp32-cvm-pubkey-b \
         esp32-lock-a esp32-lock-b esp32-lock-c \
         esp32-unlock-a esp32-unlock-b esp32-unlock-c \
         esp32-lock-status \
         esp32-force-unlock-a esp32-force-unlock-b esp32-force-unlock-c \
         esp32-build

esp32-flash-a: ## Flash firmware to Board A (requires lock-a)
	@$(MAKE) -C esp32 flash-a

esp32-flash-b: ## Flash firmware to Board B (requires lock-b)
	@$(MAKE) -C esp32 flash-b

esp32-flash-c: ## Flash firmware to Board C (requires lock-c)
	@$(MAKE) -C esp32 flash-c

esp32-provision-a: ## Full provision Board A (erase + fw + SPIFFS + wait, requires lock-a)
	@$(MAKE) -C esp32 provision-a

esp32-provision-b: ## Full provision Board B (requires lock-b)
	@$(MAKE) -C esp32 provision-b

esp32-provision-c: ## Full provision Board C (requires lock-c)
	@$(MAKE) -C esp32 provision-c

esp32-reset-a: ## Reset Board A without reflashing (requires lock-a)
	@$(MAKE) -C esp32 reset-a

esp32-reset-b: ## Reset Board B without reflashing (requires lock-b)
	@$(MAKE) -C esp32 reset-b

esp32-reset-c: ## Reset Board C without reflashing (requires lock-c)
	@$(MAKE) -C esp32 reset-c

esp32-wait-ready-a: ## Wait for Board A :2121 to be ready
	@$(MAKE) -C esp32 wait-ready-a

esp32-wait-ready-b: ## Wait for Board B :2121 to be ready
	@$(MAKE) -C esp32 wait-ready-b

esp32-wait-ready-c: ## Wait for Board C :2121 to be ready
	@$(MAKE) -C esp32 wait-ready-c

esp32-monitor-a: ## Serial monitor Board A (requires lock-a)
	@$(MAKE) -C esp32 monitor-a

esp32-monitor-b: ## Serial monitor Board B (requires lock-b)
	@$(MAKE) -C esp32 monitor-b

esp32-monitor-c: ## Serial monitor Board C (requires lock-c)
	@$(MAKE) -C esp32 monitor-c

esp32-connect-a: ## Connect laptop to Board A AP (requires lock-a)
	@$(MAKE) -C esp32 connect-a

esp32-connect-b: ## Connect laptop to Board B AP (requires lock-b)
	@$(MAKE) -C esp32 connect-b

esp32-disconnect: ## Disconnect from board AP
	@$(MAKE) -C esp32 disconnect

esp32-test-discovery-a: ## Test discovery on Board A (requires lock-a)
	@$(MAKE) -C esp32 test-discovery-a

esp32-test-discovery-b: ## Test discovery on Board B (requires lock-b)
	@$(MAKE) -C esp32 test-discovery-b

esp32-test-mints-a: ## Test /mints on Board A (requires lock-a)
	@$(MAKE) -C esp32 test-mints-a

esp32-test-mints-b: ## Test /mints on Board B (requires lock-b)
	@$(MAKE) -C esp32 test-mints-b

esp32-test-multi-mint-a: ## Full multi-mint test on Board A (requires lock-a)
	@$(MAKE) -C esp32 test-multi-mint-a

esp32-test-multi-mint-b: ## Full multi-mint test on Board B (requires lock-b)
	@$(MAKE) -C esp32 test-multi-mint-b

esp32-test-all-boards: ## Test both boards (requires lock-a + lock-b)
	@$(MAKE) -C esp32 test-all-boards

 esp32-build: ## Build ESP32 firmware (no lock)
	@$(MAKE) -C esp32 build

esp32-test-cvm-a: ## CVM announcement test on Board A (requires lock-a)
	@$(MAKE) -C esp32 test-cvm-a

esp32-test-cvm-b: ## CVM announcement test on Board B (requires lock-b)
	@$(MAKE) -C esp32 test-cvm-b

esp32-test-cvm-mcp-a: ## MCP tools/call test on Board A (requires lock-a)
	@$(MAKE) -C esp32 test-cvm-mcp-a

esp32-cvm-pubkey-a: ## Print Board A's CVM npub (no lock)
	@$(MAKE) -C esp32 cvm-pubkey-a

esp32-cvm-pubkey-b: ## Print Board B's CVM npub (no lock)
	@$(MAKE) -C esp32 cvm-pubkey-b

esp32-lock-a: ## Acquire Board A lock (PHASE='description')
	@$(MAKE) -C esp32 lock-a PHASE="$(PHASE)"

esp32-lock-b: ## Acquire Board B lock
	@$(MAKE) -C esp32 lock-b PHASE="$(PHASE)"

esp32-lock-c: ## Acquire Board C lock
	@$(MAKE) -C esp32 lock-c PHASE="$(PHASE)"

esp32-unlock-a: ## Release Board A lock
	@$(MAKE) -C esp32 unlock-a

esp32-unlock-b: ## Release Board B lock
	@$(MAKE) -C esp32 unlock-b

esp32-unlock-c: ## Release Board C lock
	@$(MAKE) -C esp32 unlock-c

esp32-lock-status: ## Show all ESP32 board lock statuses
	@$(MAKE) -C esp32 lock-status

esp32-force-unlock-a: ## Force-release Board A lock
	@$(MAKE) -C esp32 force-unlock-a

esp32-force-unlock-b: ## Force-release Board B lock
	@$(MAKE) -C esp32 force-unlock-b

esp32-force-unlock-c: ## Force-release Board C lock
	@$(MAKE) -C esp32 force-unlock-c

# ===========================================================================
#  LOCAL RELAY TESTS (Board B, feature/local-relay)
# ===========================================================================

 .PHONY: relay-build relay-flash-b relay-connect-b \
         relay-test-smoke relay-test-nip11 relay-test-pubsub \
         relay-test-sync relay-test-full

relay-build: ## Build relay firmware (no lock)
	@$(MAKE) -C esp32 relay-build

relay-flash-b: ## Flash relay firmware to Board B (requires lock-b)
	@$(MAKE) -C esp32 relay-flash-b

relay-connect-b: ## WiFi connect to Board B AP (requires lock-b)
	@$(MAKE) -C esp32 relay-connect-b

relay-test-smoke: ## Relay smoke test: port 4869 reachable (requires lock-b)
	@$(MAKE) -C esp32 relay-test-smoke

relay-test-nip11: ## NIP-11 relay info document test (requires lock-b)
	@$(MAKE) -C esp32 relay-test-nip11

relay-test-pubsub: ## WebSocket publish + subscribe test (requires lock-b)
	@$(MAKE) -C esp32 relay-test-pubsub

relay-test-sync: ## Verify sync to public relays (requires lock-b)
	@$(MAKE) -C esp32 relay-test-sync

relay-test-full: ## Run all relay tests (~1min, requires lock-b)
	@$(MAKE) -C esp32 relay-test-full

# ===========================================================================
#  ARCH COMPONENT EXTRACTION TESTS (tollgate_core on Board A)
# ===========================================================================

arch-build: ## Build arch (tollgate_core) firmware
	@$(MAKE) -C esp32 arch-build

arch-flash-a: ## Flash arch firmware to Board A (requires lock)
	@$(MAKE) -C esp32 arch-flash-a

arch-generate-spiffs: ## Generate SPIFFS with auto-detected WPA mode
	@$(MAKE) -C esp32 arch-generate-spiffs

arch-flash-spiffs-a: ## Flash SPIFFS to Board A with WPA config (requires lock)
	@$(MAKE) -C esp32 arch-flash-spiffs-a

arch-monitor-a: ## Serial monitor for arch Board A (requires lock)
	@$(MAKE) -C esp32 arch-monitor-a

arch-bootlog-a: ## Capture boot log from Board A (requires lock)
	@$(MAKE) -C esp32 arch-bootlog-a

arch-connect-a: ## WiFi connect to Board A AP
	@$(MAKE) -C esp32 arch-connect-a

arch-disconnect: ## Disconnect WiFi from Board A AP
	@$(MAKE) -C esp32 arch-disconnect

arch-test-smoke: ## Smoke test (~30s)
	@$(MAKE) -C esp32 arch-test-smoke

arch-test-network: ## Network test (~15s)
	@$(MAKE) -C esp32 arch-test-network

arch-test-api: ## API endpoint test (~20s)
	@$(MAKE) -C esp32 arch-test-api

arch-test-dns-fw: ## DNS + Firewall test (~30s)
	@$(MAKE) -C esp32 arch-test-dns-fw

arch-test-reset: ## Reset auth cycle test (~30s)
	@$(MAKE) -C esp32 arch-test-reset

arch-test-session: ## Session expiry test (~80s)
	@$(MAKE) -C esp32 arch-test-session

arch-test-phase2: ## Phase 2 API test (~90s)
	@$(MAKE) -C esp32 arch-test-phase2

arch-test-cleanup: ## Disconnect WiFi + reset auth
	@$(MAKE) -C esp32 arch-test-cleanup

arch-test-full: ## Run all arch E2E tests (~4min)
	@$(MAKE) -C esp32 arch-test-full

# ===========================================================================
#  PYTEST / CI / REPORT TARGETS (from main branch)
# ===========================================================================

.PHONY: pytest-smoke pytest-critical pytest-extended pytest-api pytest-phone \
        pytest-test pytest-scenarios pytest-hardware-smoke pymake-help \
        install-path-preflight install-path-dry-run install-path-e2e \
        fresh-flash-check bench-lock-status \
        pytest-smoke-mac pytest-critical-mac pytest-api-mac pytest-test-mac \
        pytest-smoke-linux pytest-api-linux pytest-test-linux \
        pytest-smoke-rust pytest-api-rust pytest-test-rust pytest-critical-rust \
        pytest-smoke-rust-basic pytest-api-rust-basic \
        luci deploy-ci deploy-ci-rust setup-python \
        run-api run-api-quick run-phone run-captive-portal run-luci run-all run-profile \
        collect render-report sanitize publish pr-smoke clean

# --- Pytest test tiers (raw pytest, no canonical run dir) ---

pytest-smoke:
	pytest -m smoke

pytest-critical:
	pytest -m critical

pytest-extended:
	pytest -m extended

pytest-api:
	pytest -m api

pytest-phone:
	pytest -m phone --publish

pytest-test:
	pytest

pytest-scenarios: ## Hardware scenario tests (requires lock + routers.env)
	$(call require_hardware_lock)
	@TOLLGATE_USE_HARDWARE_LOCK=1 pytest tests/scenarios/ -m hardware -v --tb=short

# --- Dual-install-path e2e (fresh flash -> direct package / installer -> happy path) ---
#
# The bench MT3000 is a SINGLE-OWNER resource: every router-touching step runs
# under the sanctioned bench lock (`bench-with-lock` / `bench-deploy-apk`, see
# the tollgate-development skill reference `bench-router-single-owner`).  When
# that helper is not installed the lock is taken in-process by lib/bench_lock.py
# on the same flock + the same holder-line format.

install-path-preflight: ## Flash-free, network-free: does this release support the POLICY/guard assertion? [exit 3 = unsupported]
	@PYTHONPATH=. python3 -c "import os; from lib import install_paths as ip; r = ip.policy_preflight(os.environ.get('TOLLGATE_FEED_TAG') or ip.FEED_RELEASE_DEFAULT); print(r.message()); raise SystemExit(0 if r.supported else 3)"

install-path-dry-run: ## Flash-free dual-install-path checks: pre-flight, artifact fetch+hash, same-format payload identity, installer shape, image verify, lock state
	@if command -v bench-with-lock >/dev/null 2>&1; then \
		bench-with-lock --purpose "install-path dry run" --task $${TOLLGATE_BENCH_TASK_ID:-t_a05094ad} -- \
			env PYTHONPATH=. TOLLGATE_APK_TOOL=$${TOLLGATE_APK_TOOL:-$$HOME/.cache/apk-v3/apk.static} \
			python3 scripts/install-path-e2e.py --dry-run --host $(TOLLGATE_SSH_HOST) \
			--md-out docs/install-paths-dry-run-report.md; \
	else \
		echo "note: bench-with-lock not installed — using the in-process lock (lib/bench_lock.py)"; \
		env PYTHONPATH=. TOLLGATE_APK_TOOL=$${TOLLGATE_APK_TOOL:-$$HOME/.cache/apk-v3/apk.static} \
			python3 scripts/install-path-e2e.py --dry-run --host $(TOLLGATE_SSH_HOST) \
			--md-out docs/install-paths-dry-run-report.md; \
	fi

install-path-e2e: ## LOCKED bench phase: wallet gate + bench lock + fresh flash + both install paths + happy path (needs TOLLGATE_LN_ADDRESS + TOLLGATE_ENABLE_SYSUPGRADE_FLASHING=true)
	$(call require_hardware_lock)
	@PYTHONPATH=. python3 scripts/install-path-e2e.py --flash-and-run --host $(TOLLGATE_SSH_HOST)

fresh-flash-check: ## Read-only fresh-flash preconditions (image hash, wallet drain gate, lock state)
	@PYTHONPATH=. python3 scripts/fresh-flash.py --check

bench-lock-status: ## Show who holds the shared bench lock (~/.hermes/state/bench-mt3000.lock)
	@python3 -m lib.bench_lock status

# --- Second-purchase bench lanes (procedure + measured result: docs/second-purchase-bench.md)
#
# Every router-touching step rides the sanctioned single-owner bench lock; the e2e takes it
# itself (re-exec under `bench-lock.sh exec`) unless it is already inside a window.
# NOTHING HERE SPENDS ANYTHING unless you pass ARGS=--purchase (the e2e) or ARGS=--yes (mint).

SECOND_PURCHASE_ARGS ?=
BENCH_TOKEN_AMOUNT   ?= 64
BENCH_TOKEN_ARGS     ?=
TOKEN_FILE           ?=

.PHONY: second-purchase-e2e second-purchase-detached bench-snapshot bench-snapshot-payload \
        bench-token-mint bench-token-verify bench-tests

second-purchase-e2e: ## Second purchase on the bench: DRY RUN default (ARGS=--purchase TOKEN_1=.. TOKEN_2=.. spends)
	@bash scripts/mt3000-bench/second-purchase-e2e.sh $(SECOND_PURCHASE_ARGS)

second-purchase-detached: ## Launch the long second-purchase run detached (systemd-run --user), then poll the journal
	@systemd-run --user --collect --unit=tg-second-purchase-$$(date +%s) \
		bash scripts/mt3000-bench/second-purchase-e2e.sh $(SECOND_PURCHASE_ARGS)

bench-snapshot: ## Router-side snapshot: ndsctl, nft guard counters, /balance, module log (needs the bench window)
	@bash scripts/mt3000-bench/router-snapshot.sh snapshot --label "make bench-snapshot"

bench-snapshot-payload: ## Print the on-router snapshot payload locally (no ssh, no lock) — review what runs on the box
	@bash scripts/mt3000-bench/router-snapshot.sh render

bench-token-mint: ## Mint one token for the bench (DRY RUN; ARGS=--yes to actually mint)
	@scripts/mt3000-bench/bench-token.py mint --amount $(BENCH_TOKEN_AMOUNT) $(BENCH_TOKEN_ARGS)

bench-token-verify: ## NUT-07: TOKEN_FILE must read back fully UNSPENT before it is spent (exit 1 if not)
	@test -n "$(TOKEN_FILE)" || { echo "set TOKEN_FILE=<path to a cashu token file>"; exit 1; }
	@scripts/mt3000-bench/bench-token.py verify --token-file $(TOKEN_FILE) $(BENCH_TOKEN_ARGS)

bench-tests: ## Offline negative-control suite for the bench lock, the e2e lanes and the snapshot payload (no router)
	@bash tests/mt3000-bench/run-tests.sh

pytest-hardware-smoke: ## Migrated smoke-* scenario subset
	$(call require_hardware_lock)
	@TOLLGATE_USE_HARDWARE_LOCK=1 ./scripts/pymake.py smoke-degraded --router $(ROUTER)

pymake-help: ## List targets available via ./scripts/pymake.py
	@./scripts/pymake.py help

# --- macOS client (no phone) ---

pytest-smoke-mac:
	pytest -m smoke --client=mac

pytest-critical-mac:
	pytest -m critical --client=mac

pytest-api-mac:
	pytest -m api --client=mac

pytest-test-mac:
	pytest --client=mac

# --- Linux client (no phone) ---

pytest-smoke-linux:
	pytest -m smoke --client=linux

pytest-api-linux:
	pytest -m api --client=linux

pytest-test-linux:
	pytest --client=linux

# --- Rust v1 backend ---

pytest-smoke-rust:
	TOLLGATE_BACKEND=rust pytest -m smoke --backend=rust

pytest-api-rust:
	TOLLGATE_BACKEND=rust pytest -m api --backend=rust

pytest-test-rust:
	TOLLGATE_BACKEND=rust pytest --backend=rust

pytest-critical-rust:
	TOLLGATE_BACKEND=rust pytest -m critical --backend=rust

# --- Rust basic backend ---

pytest-smoke-rust-basic:
	TOLLGATE_BACKEND=rust-basic pytest -m smoke --backend=rust-basic

pytest-api-rust-basic:
	TOLLGATE_BACKEND=rust-basic pytest -m api --backend=rust-basic

# --- Canonical run dir targets ---

run-profile:
	./scripts/run-profile.sh --profile "$(PROFILE)"

run-api:
	PROFILE="${PROFILE:-virtual-lab-api}" ./scripts/run-api.sh

run-api-quick:
	./scripts/run-profile.sh --profile virtual-lab-api-quick

run-phone:
	PROFILE="${PROFILE:-physical-phone-captive-portal}" ./scripts/run-phone.sh

run-captive-portal:
	./scripts/run-profile.sh --profile virtual-lab-captive-portal

run-luci:
	PROFILE="${PROFILE:-virtual-lab-luci}" ./scripts/run-tests.sh

run-all:
	PROFILE="${PROFILE:-virtual-lab-api}" ./scripts/run-all.sh

# --- Playwright LuCI tests (legacy) ---

luci:
	npx playwright test

# --- Collect and render from latest run ---

RESULTS_DIR := results

collect:
	@run=$$(ls -dt $(RESULTS_DIR)/*/ 2>/dev/null | head -1); \
	if [ -z "$$run" ]; then echo "No results to collect"; exit 1; fi; \
	python3 scripts/collect-results.py --run-dir "$$run" --allow-failures

render-report:
	@run=$$(ls -dt $(RESULTS_DIR)/*/ 2>/dev/null | head -1); \
	if [ -z "$$run" ]; then echo "No results to render"; exit 1; fi; \
	python3 scripts/render-report.py --run-dir "$$run"

# --- CI artifact deploy ---

deploy-ci:
	bash scripts/deploy-ci.sh

deploy-ci-rust:
	bash scripts/deploy-rust-ci.sh

# --- Feed RC verification (FreedomTechFeed/packages) ---
#
# Installs the feed-built tollgate-wrt release candidate over the router's own
# package manager and asserts version + build identity + service health:
#   * OpenWrt 24.x -> opkg -> .ipk    (FORMAT=ipk, default)
#   * OpenWrt 25.x -> apk  -> .apk    (FORMAT=apk)
# The artifact is downloaded from the feed release (sha256-verified) by
# scripts/download-feed-release.sh. Requires the hardware lock.

FEED_RELEASE_TAG ?= v0.6.0-alpha2-pre
FEED_EXPECT_VERSION ?= 0.6.0_alpha2_pre-r1
FEED_EXPECT_COMMIT ?= 089e876cb24fd2fa8bd9d36323edb71e347e825b
FORMAT ?= ipk
PYTHON ?= python3
PYTEST_ARGS ?=

.PHONY: verify-feed-rc
verify-feed-rc: ## [hardware] Verify the FreedomTechFeed RC installs+works (.ipk on 24.x, .apk on 25.x)
	$(call require_hardware_lock)
	@test -n "$${TOLLGATE_SSH_HOST:-}" || { echo "$(RED)Set TOLLGATE_SSH_HOST (e.g. 192.168.1.1)$(RESET)"; exit 1; }
	@test -n "$${TOLLGATE_SSH_PASSWORD:-}" || { echo "$(RED)Set TOLLGATE_SSH_PASSWORD$(RESET)"; exit 1; }
	@ARCH="$${TOLLGATE_ROUTER_ARCH:-aarch64_cortex-a53}"; \
	PKG=$$(bash scripts/download-feed-release.sh "$(FEED_RELEASE_TAG)" "$$ARCH" "$(FORMAT)"); \
	echo "$(BOLD)=== verify-feed-rc: $$PKG (format=$(FORMAT), arch=$$ARCH) ===$(RESET)"; \
	TOLLGATE_PACKAGE_PATH="$$PKG" \
	TOLLGATE_ROUTER_ARCH="$$ARCH" \
	FEED_EXPECT_VERSION="$(FEED_EXPECT_VERSION)" \
	FEED_EXPECT_COMMIT="$(FEED_EXPECT_COMMIT)" \
	$(PYTHON) -m pytest tests/scenarios/test_feed_package.py -v -s $(PYTEST_ARGS)

# --- Setup (Python venv) ---

setup-python:
	bash scripts/setup-python.sh

# --- Results pipeline ---

sanitize:
	@run=$$(ls -dt $(RESULTS_DIR)/*/ 2>/dev/null | head -1); \
	if [ -z "$$run" ]; then echo "No results to sanitize"; exit 1; fi; \
	bash scripts/sanitize-results.sh "$$run"

publish:
	@run=$$(ls -dt $(RESULTS_DIR)/*/ 2>/dev/null | head -1); \
	if [ -z "$$run" ]; then echo "No results to publish"; exit 1; fi; \
	bash scripts/publish-report.sh "$$run"

# --- PR smoke test ---

pr-smoke:
	@echo "Usage: ./scripts/test-pr.sh --pr <N> [--reset] [--test api|all] [--publish]"

# --- PR #120: Mint resilience test suite ---

smoke-pr120: ## Quick smoke for PR #120 features (try-all-mints + CLI degraded ops)
	pytest tests/api/test_try_all_mints.py tests/api/test_cli_degraded_operations.py -v --timeout=120

full-pr120: ## Full PR #120 test suite (includes recovery lifecycle ~10 min)
	pytest tests/api/test_try_all_mints.py tests/api/test_merchant_provider.py \
		tests/api/test_recovery_lifecycle.py tests/api/test_cli_degraded_operations.py \
		-v --timeout=600

pr120-recovery: ## Recovery lifecycle tests only
	pytest tests/api/test_recovery_lifecycle.py -v --timeout=600

# --- Portal demo recording (no hardware required — local mock server) ---
# Records desktop + mobile portal walkthrough videos with a glowing cursor
# overlay so demos are easy to follow. Requires `npm install` first.
# Override the portal source with PORTAL_DIR= and output with VIDEO_DIR=.

.PHONY: record-portal

record-portal: ## Record portal demo videos WITH cursor highlight (desktop+mobile)
	@if [ ! -d node_modules ]; then echo "$(YELLOW)Run npm install first$(RESET)"; exit 1; fi
	@node scripts/record-portal-highlight.mjs

# --- Pre-auth probes ---

.PHONY: probe-ports
probe-ports: ## Pre-auth port/UI sweep from a captive-LAN client (no SSH, no creds)
	@bash scripts/tollgate-port-sweep.sh $(if $(HOST),--host $(HOST),)

# --- Clean ---

clean:
	rm -rf $(RESULTS_DIR)/*
	rm -f report.html
	rm -rf .pytest_cache __pycache__

# ─── User-story tests (device-agnostic, labgrid-integrated) ─────────────

.PHONY: pytest-stories pytest-story-pay pytest-story-expiry pytest-story-degraded

pytest-stories:
	pytest tests/stories/ --no-deploy --timeout-method=signal -v

pytest-story-pay:
	pytest tests/stories/test_user_pays_and_gets_internet.py --no-deploy --timeout-method=signal -v

pytest-story-expiry:
	pytest tests/stories/test_session_expiry_and_repayment.py --no-deploy --timeout-method=signal -v

pytest-story-degraded:
	pytest tests/stories/test_degraded_mode.py --no-deploy --timeout-method=signal -v
