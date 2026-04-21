#!/bin/bash

HOST="${1:-127.0.0.1}"
PORT="${2:-6379}"

VALKEY_CLI="${VALKEY_CLI:-$HOME/valkey-8.1.1/src/valkey-cli}"
CLI="$VALKEY_CLI -h $HOST -p $PORT --tls"

echo "Starting full keyspace scan on $HOST:$PORT"
echo "Before scan:"
$CLI INFO memory | grep used_memory_human
$CLI INFO keyspace
echo ""

CURSOR=0
TOTAL=0
START=$(date +%s)

while true; do
    RESULT=$($CLI SCAN $CURSOR COUNT 1000)
    CURSOR=$(echo "$RESULT" | head -1)
    BATCH=$(echo "$RESULT" | tail -n +2 | wc -l)
    TOTAL=$((TOTAL + BATCH))

    if [ $((TOTAL % 100000)) -lt 1000 ]; then
        echo "Scanned ~$TOTAL keys so far... (cursor: $CURSOR)"
    fi

    if [ "$CURSOR" = "0" ]; then
        break
    fi
done

END=$(date +%s)
ELAPSED=$((END - START))

echo ""
echo "Scan complete: ~$TOTAL keys visited in ${ELAPSED}s"
echo ""
echo "After scan:"
$CLI INFO memory | grep used_memory_human
$CLI INFO keyspace
