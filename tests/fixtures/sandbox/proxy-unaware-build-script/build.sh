#!/bin/sh
# downloads with its own curl and never looks at the proxy variables
set -e
curl -fsS --noproxy '*' --max-time 60 -o /tmp/simple.html https://pypi.org/simple/pip/
grep -c 'pip-' /tmp/simple.html
