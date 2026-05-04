.PHONY: validate install uninstall test test-e2e test-all help

MARKETPLACE := agent-eval-harness-dev
PLUGIN := agent-eval-harness@$(MARKETPLACE)

validate:
	claude plugin validate ./

install:
	@claude plugin rm $(PLUGIN) 2>/dev/null || true
	@claude plugin marketplace rm $(MARKETPLACE) 2>/dev/null || true
	@claude plugin marketplace add ./ && echo "Marketplace added."
	claude plugin install $(PLUGIN)

uninstall:
	@echo "Removing plugin..."
	@claude plugin rm $(PLUGIN) 2>/dev/null || echo "Plugin not installed"
	@echo "Removing marketplace..."
	@claude plugin marketplace rm $(MARKETPLACE) 2>/dev/null || echo "Marketplace not installed"

test:
	python3 -m pytest tests/ -v

test-e2e:
	python3 -m pytest tests/e2e/ -v -s -m e2e

test-all:
	python3 -m pytest tests/ -v -s -m ""

help:
	@echo "Available targets:"
	@echo "  validate   - Validate plugin manifest"
	@echo "  install    - Install plugin persistently via local marketplace"
	@echo "  uninstall  - Remove plugin and local marketplace"
	@echo "  test       - Run unit tests"
	@echo "  test-e2e   - Run e2e tests (requires ANTHROPIC_API_KEY)"
	@echo "  test-all   - Run all tests"
