PYTHON ?= python3
.PHONY: test test-quick check test-install package release install

test: check

check:
	$(PYTHON) scripts/check.py

test-quick:
	$(PYTHON) -m unittest discover -s tests -q
	node --test tests/bridge.test.mjs

test-install:
	./scripts/test-install.sh

package:
	$(PYTHON) scripts/package.py --output dist

release:
	$(PYTHON) scripts/package.py --output dist --release

install:
	./scripts/setup.sh
