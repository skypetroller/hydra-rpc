PREFIX ?= /usr/local/bin
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
AUTOSTART_DIR ?= $(HOME)/.config/autostart

.PHONY: install install-user uninstall test lint validate-desktop check help

help:
	@echo "Targets:"
	@echo "  install         - install to \$$(PREFIX)/hydra-rpc (default: $(PREFIX), needs sudo for /usr/local)"
	@echo "  install-user    - install to ~/.local/bin/hydra-rpc (no sudo)"
	@echo "  uninstall       - remove installed script"
	@echo "  test            - run unit tests"
	@echo "  lint            - syntax check + schema/example JSON validation"
	@echo "  check           - test + lint"
	@echo "  validate-desktop- validate hydra-rpc.desktop (needs desktop-file-utils)"

install:
	install -Dm755 hydra-rpc $(DESTDIR)$(PREFIX)/hydra-rpc
	@echo "Installed to $(DESTDIR)$(PREFIX)/hydra-rpc"
	@echo "Next: hydra-rpc --validate-config"

install-user:
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

validate-desktop:
	desktop-file-validate hydra-rpc.desktop

check: test lint
