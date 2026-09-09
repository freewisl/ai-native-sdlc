#!/bin/bash
# DO NOT EDIT during a fix task. Proves the bug is gone.
[ "$(bash ./impl.sh 4)" = "10" ] || { echo "FAIL: sum(1..4) expected 10 got $(bash ./impl.sh 4)"; exit 1; }
[ "$(bash ./impl.sh 1)" = "1" ]  || { echo "FAIL: sum(1..1) expected 1"; exit 1; }
echo "PASS"
