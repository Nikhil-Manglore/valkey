#!/bin/bash

HOST="${1:-127.0.0.1}"
PORT="${2:-6379}"
TTL="${3:-180}"

VALKEY_BENCH="${VALKEY_BENCH:-$HOME/valkey-8.1.1/src/valkey-benchmark}"
VALKEY_CLI="${VALKEY_CLI:-$HOME/valkey-8.1.1/src/valkey-cli}"
CLI="$VALKEY_CLI -h $HOST -p $PORT --tls"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

PAD=$(python3 -c "print('x' * 2048)")


if ! $CLI PING > /dev/null 2>&1; then
    echo "ERROR: Cannot connect to $HOST:$PORT"
    exit 1
fi

trap "kill 0; exit" SIGINT SIGTERM

# Primary: Clustered key workload (64 workers)
python3 "$SCRIPT_DIR/lua_workload.py" $HOST $PORT $TTL 64 &

# Additional SETEX pressure with large values
for i in 1 2; do
    while true; do
        $VALKEY_BENCH -h $HOST -p $PORT --tls \
            -n 100000000 -c 32 -P 8 --threads 2 -r 500000000 \
            -q SETEX __rand_int__ $TTL "${PAD}__rand_int__"
    done &
done

wait
