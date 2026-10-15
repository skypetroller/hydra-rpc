PREFIX ?= /usr/local/bin
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
AUTOSTART_DIR ?= $(HOME)/.config/autostart

.PHONY: install install-user uninstall test lint validate-desktop check help build verify sha256 status-contract schema-check

help:
	@echo "Targets:"
	@echo "  build           - regenerate ./hydra-rpc bundle from src/ (verified AST-identical)"
	@echo "  verify          - fail if bundle drifts from src/ or sha256 is stale"
	@echo "  sha256          - (re)generate hydra-rpc.sha256 for the updater"
	@echo "  install         - build + install to \$$(PREFIX)/hydra-rpc (needs sudo for /usr/local)"
	@echo "  install-user    - build + install to ~/.local/bin/hydra-rpc (no sudo)"
	@echo "  uninstall       - remove installed script"
	@echo "  test            - run unit tests"
	@echo "  lint            - syntax + JSON + schema/example validation"
	@echo "  status-contract - live --status --json shape check"
	@echo "  schema-check    - config.example.json vs hydra-rpc.schema.json"
	@echo "  check           - verify + test + lint + status-contract"

build:
	python3 build.py

verify:
	python3 build.py --check
	python3 scripts/check_sha.py --check

sha256:
	python3 scripts/check_sha.py --generate
	@echo "Wrote hydra-rpc.sha256 (commit it so the updater can verify)"

install: build
	install -Dm755 hydra-rpc $(DESTDIR)$(PREFIX)/hydra-rpc
	@echo "Installed to $(DESTDIR)$(PREFIX)/hydra-rpc"
	@echo "Next: hydra-rpc --validate-config"

install-user: build
	mkdir -p $(HOME)/.local/bin
	install -Dm755 hydra-rpc $(HOME)/.local/bin/hydra-rpc
	@echo "Installed to $(HOME)/.local/bin/hydra-rpc"
	@echo "Make sure ~/.local/bin is on PATH, then run: hydra-rpc --validate-config"
	@echo "NOTE: hydra-rpc.service / hydra-rpc.desktop assume /usr/local/bin/hydra-rpc;"
	@echo "      edit Exec=/ExecStart= if you use ~/.local/bin."

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/hydra-rpc $(HOME)/.local/bin/hydra-rpc
	@echo "Removed hydra-rpc (if present). Disable service/autostart separately if enabled."

test:
	python3 -m unittest discover --start-directory tests --verbose

lint:
	python3 -B -c "import ast; ast.parse(open('hydra-rpc').read()); print('syntax OK')"
	python3 -c "import json; json.load(open('hydra-rpc.schema.json')); json.load(open('config.example.json')); print('JSON OK')"
	python3 scripts/check_config_schema.py

schema-check:
	python3 scripts/check_config_schema.py

status-contract:
	python3 scripts/check_status_contract.py

validate-desktop:
	desktop-file-validate hydra-rpc.desktop

check: verify test lint status-contract
