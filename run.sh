#!/bin/bash
# Run the extraction pipeline, then open the dashboard.
# Re-running is cheap: LLM answers are cached and human decisions stay in data/overrides.csv.
set -euo pipefail
cd "$(dirname "$0")"

python3 src/pipeline.py
exec python3 -m streamlit run src/dashboard.py
