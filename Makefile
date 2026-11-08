.PHONY: lint test vendor

PYTHON ?= python3
# Every folder whose tests/ holds Python tests: shared/ and each plugin's or skill's.
SUITES := $(sort $(patsubst %/tests/,%,$(dir $(shell find shared plugins -path '*/tests/test_*.py'))))

lint:
	$(PYTHON) tools/validate.py

test:
	@for suite in $(SUITES); do \
		echo "== $$suite"; \
		(cd $$suite && PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests) || exit 1; \
	done
	node --test 'plugins/**/tests/*.test.mjs'

vendor:
	$(PYTHON) tools/vendor.py
