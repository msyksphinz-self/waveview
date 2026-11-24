#!/bin/sh
# Self tests of the viewer, the query tool and the RTL index.
set -e
cd "$(dirname "$0")"
python3 rtlindex.py --selftest
python3 ifaces.py
python3 wavequery.py --selftest
python3 waveview.py selftest
