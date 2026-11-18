#!/bin/sh
# Copies the aiointernxt library into the integration until it is published on PyPI.
set -eu
cd "$(dirname "$0")/.."
rm -rf custom_components/internxt/aiointernxt
mkdir -p custom_components/internxt/aiointernxt
cp aiointernxt/src/aiointernxt/*.py aiointernxt/src/aiointernxt/py.typed custom_components/internxt/aiointernxt/
echo "aiointernxt synced into custom_components/internxt/aiointernxt"
