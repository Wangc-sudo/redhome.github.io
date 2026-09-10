#!/usr/bin/env bash
set -euo pipefail

export INTEGRATION_TEST_RUNNER=1
python3 -m unittest discover -s tests -t . -v
