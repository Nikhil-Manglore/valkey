#!/usr/bin/env python3

import sys
import time
import random
import threading
import redis

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 6379
TTL = int(sys.argv[3]) if len(sys.argv) > 3 else 60
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 64

# Pre-generate big value payloads to avoid repeated allocation
BIG_VALUE_50K = "x" * 50000
BIG_VALUE_200K = "x" * 200000
BIG_VALUE_1M = "x" * 1000000
SMALL_VALUE = "x" * 100  # avg ~100 bytes like customer

# Lua script: EXISTS + SMEMBERS + EXISTS + PUBLISH (customer's main pattern)
LUA_READ_PUBLISH = """
local exists1 = redis.call('exists', KEYS[1])
local members = redis.call('smembers', KEYS[1])
local exists2 = redis.call('exists', KEYS[2])
redis.call('publish', 'notifications', 'event')
return #members
"""

# Lua script: INCR + PEXPIRE (counter pattern)
LUA_COUNTER = """
local val = redis.call('incr', KEYS[1])
redis.call('pexpire', KEYS[1], ARGV[1])
return val
"""

# Lua script: heavy multi-command
LUA_HEAVY = """
local exists1 = redis.call('exists', KEYS[1])
if exists1 == 1 then
    local members = redis.call('smembers', KEYS[1])
    local exists2 = redis.call('exists', KEYS[2])
    redis.call('publish', 'notifications', 'processed')
    redis.call('incr', KEYS[3])
    redis.call('pexpire', KEYS[3], ARGV[1])
    return #members
end
return 0
"""


def get_string_value(i):
    r = i % 1000
    if r < 980:
        return SMALL_VALUE
    elif r < 995:
        return BIG_VALUE_50K
    elif r < 999:
        return BIG_VALUE_200K
    else:
        return BIG_VALUE_1M


def get_set_members(i):
    r = i % 1000
    if r < 900:
        return ["m1"]  # 1 member (most common)
    elif r < 970:
        return ["m1", "m2"]  # 2 members
    elif r < 995:
        return [f"m{j}" for j in range(10)]  # 10 members
    elif r < 999:
        return [f"m{j}" for j in range(100)]  # 100 members
    else:
        return [f"m{j}" for j in range(500)]  # 500 members (rare big set)


def write_worker(worker_id, host, port, ttl):
    r = redis.Redis(host=host, port=port, ssl=True, socket_timeout=5)
    batch_id = worker_id
    batch_size = 5000
    ops = 0
    start = time.time()

    while True:
        try:
            prefix = f"batch{batch_id:08d}"
            pipe = r.pipeline()

            for i in range(batch_size):
                if i % 100 < 84:
                    # String with TTL
                    k = f"{prefix}:str:{i}"
                    pipe.setex(k, ttl, get_string_value(i))
                else:
                    # Set with TTL
                    k = f"{prefix}:set:{i}"
                    members = get_set_members(i)
                    pipe.delete(k)
                    pipe.sadd(k, *members)
                    pipe.expire(k, ttl)

                if (i + 1) % 500 == 0:
                    pipe.execute()
                    pipe = r.pipeline()

            pipe.execute()
            ops += batch_size
            batch_id += WORKERS

            if ops % 50000 == 0:
                elapsed = time.time() - start
                rate = ops / elapsed
                print(f"[Write {worker_id:2d}] {ops:>10d} ops | {rate:>8.0f} ops/sec | batch={batch_id}")

        except redis.exceptions.ConnectionError:
            time.sleep(1)
        except Exception as e:
            pass


def lua_worker(worker_id, host, port, ttl):
    r = redis.Redis(host=host, port=port, ssl=True, socket_timeout=5)
    ttl_ms = ttl * 1000

    script_read = r.register_script(LUA_READ_PUBLISH)
    script_counter = r.register_script(LUA_COUNTER)
    script_heavy = r.register_script(LUA_HEAVY)

    ops = 0
    start = time.time()
    max_batch = 1000000

    while True:
        try:
            batch = random.randint(0, max_batch)
            prefix = f"batch{batch:08d}"
            idx = random.randint(0, 4999)

            roll = random.random()

            if roll < 0.35:
                k1 = f"{prefix}:set:{idx}"
                k2 = f"{prefix}:str:{idx}"
                try:
                    script_read(keys=[k1, k2])
                except redis.exceptions.ResponseError:
                    pass

            elif roll < 0.55:
                k1 = f"{prefix}:str:{idx}"
                try:
                    script_counter(keys=[k1], args=[ttl_ms])
                except redis.exceptions.ResponseError:
                    pass

            elif roll < 0.70:
                k1 = f"{prefix}:set:{idx}"
                k2 = f"{prefix}:str:{idx}"
                k3 = f"{prefix}:str:{idx + 1}"
                try:
                    script_heavy(keys=[k1, k2, k3], args=[ttl_ms])
                except redis.exceptions.ResponseError:
                    pass

            elif roll < 0.85:
                k = f"{prefix}:str:{idx}"
                r.get(k)

            else:
                k = f"{prefix}:set:{idx}"
                r.smembers(k)

            ops += 1
            if ops % 50000 == 0:
                elapsed = time.time() - start
                rate = ops / elapsed
                print(f"[Lua   {worker_id:2d}] {ops:>10d} ops | {rate:>8.0f} ops/sec")

        except redis.exceptions.ConnectionError:
            time.sleep(1)
        except Exception:
            pass


def main():
    num_write_workers = WORKERS // 4  # 25% writers
    num_lua_workers = WORKERS - num_write_workers  # 75% Lua

    threads = []
    for i in range(num_write_workers):
        t = threading.Thread(target=write_worker, args=(i, HOST, PORT, TTL), daemon=True)
        t.start()
        threads.append(t)

    for i in range(num_lua_workers):
        t = threading.Thread(target=lua_worker, args=(i, HOST, PORT, TTL), daemon=True)
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nStopping...")

if __name__ == "__main__":
    main()
