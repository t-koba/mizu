PYTHON ?= python3
.PHONY: test check test-install package release install

test:
	$(PYTHON) -m unittest discover -s tests -q
	node --test tests/bridge.test.mjs

check:
	$(PYTHON) scripts/check.py

test-install:
	./scripts/test-install.sh

package:
	$(PYTHON) scripts/package.py --output dist

release:
	$(PYTHON) scripts/package.py --output dist --release

install:
	./scripts/setup.sh
