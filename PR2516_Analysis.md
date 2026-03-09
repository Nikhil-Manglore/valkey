# PR #2516 Throughput Regression Analysis

## Summary

PR [#2516](https://github.com/valkey-io/valkey/pull/2516) removes internal server object pointer overhead to save 20–30% memory for small strings. The tradeoff is a ~6% throughput regression at high-throughput configurations.

This document explains exactly what causes the regression, how I measured it, and why the attempted fixes failed to recover the lost throughput.

---

## Observed Regression

| Workload | Before PR | After PR | Delta |
|---|---|---|---|
| SET 96-byte, pipeline=10, 9 IO threads, 1600 clients | ~1.86M rps | ~1.75M rps | −6% |
| GET 16-byte, pipeline=10, 9 IO threads, 1600 clients | ~2.63M rps | ~2.47M rps | −6% |

This PR was tested across multiple configurations and the regression only appears when both high pipelining and many IO threads are combined: [Dashboard Link](https://perf-dashboard.valkey.io/public-dashboards/3e45bf8ded3043edaa941331cd1a94e2?from=2026-01-05T19:00:00.000Z&to=2026-01-06T06:59:58.000Z&timezone=UTC)


This pattern tells us that the regression requires the main thread to be CPU-bound. Pipeline=10 allows the server to process many commands per event loop iteration without network round-trip delay which exposes CPU bottlenecks. Additionally, IO-threads=9 offloads read/write/free work to IO threads, making the main thread's command execution the bottleneck. When both are active, the main thread is saturated and every extra instruction directly reduces max throughput.

---

## Testing Environment

- Instance: **c7g.metal** (AWS Graviton3)
- Architecture: **aarch64**, 64 vCPUs
- Cache: 64 KiB L1d per core, 1 MiB L2 per core, 32 MiB shared L3
- Build: `make -j64` with **-O3**, no LTO (`-flto` is not active in the default Makefile build)
- Valkey config: `--io-threads 9 --io-threads-do-reads yes`
- Benchmark: `valkey-benchmark -c 1600 -P 10 -d 96 --threads 90`

---

## What the PR Changed

Before the PR, every `serverObject` (robj) had a direct pointer to its value:

```c
// BEFORE: serverObject layout
struct serverObject {
    unsigned type : 4;
    unsigned encoding : 4;
    unsigned lru : 24;
    unsigned refcount : 32;
    void *ptr;              // Direct pointer to value (SDS string)
};
```

Accessing the value was a single instruction:

```asm
ldr  x0, [x0, #8]    // o->ptr — one load then done
```

After the PR, the value can be either a pointer (`val_ptr`) or embedded inline after the object header. Three new bitfield flags (`hasexpire`, `hasembkey`, `hasembval`) control the layout:

```c
// AFTER: serverObject layout
struct serverObject {
    unsigned type : 4;
    unsigned encoding : 4;
    unsigned lru : 24;
    unsigned hasexpire : 1;
    unsigned hasembkey : 1;
    unsigned hasembval : 1;
    unsigned refcount : 29;
    void *val_ptr;          // Only valid when hasembval == 0
};
```

Accessing the value now requires calling `objectGetVal()`:

```c
void *objectGetVal(const robj *o) {
    if (o->hasembval) {
        unsigned char *data = objectEmbeddedData(o);
        if (o->hasexpire) {
            /* Skip expire field */
            data += sizeof(long long);
        }
        if (o->hasembkey) {
            /* Skip embedded key */
            uint8_t hdr_size = *(uint8_t *)data;
            data += 1 + hdr_size;                /* +1 for header size byte */
            data += sdslen((const_sds)data) + 1; /* +1 for null terminator */
        }
        assert(o->encoding == OBJ_ENCODING_EMBSTR);
        return data + sdsHdrSize(SDS_TYPE_8);
    } else {
        return o->val_ptr;
    }
}
```

The compiled fast path on aarch64 (for non-embedded objects like our 96-byte values):

```asm
objectGetVal:
  ldr   x1, [x0]              // Load 8-byte bitfield header
  tbz   x1, #34, fast_path    // Test hasembval bit — branch if clear
  ...                          // (slow embedded path: ~30 instructions + sdsHdrSize call)
fast_path:
  ldr   x0, [x0, #8]          // Load val_ptr
  ret
```

The compiler places the fast path (`val_ptr` load + return) at the end of the function body and then branches to it when `hasembval` is clear. The slow embedded path falls through inline. For non-embedded objects, this is 4 instructions (`ldr` + `tbz` + `ldr` + `ret`). For embedded objects, a typical slow-path execution is ~30 instructions plus a function call to `sdsHdrSize`.

Similarly, `objectSetVal()` is a new function for writing values, and `objectGetKey()` for reading embedded keys. Every place in the codebase that previously did `o->ptr` now calls one of these functions.

---

## Profiling

I used two profiling approaches:

### 1. Hardware Performance Counters (`perf stat`)

Measures aggregate CPU metrics on the main thread for over 300 seconds under sustained load.

```bash
# 300s warmup, then 300s collection on the valkey-server PID
sudo perf stat -p $PID \
  -e cycles,instructions,branches,branch-misses,\
     cache-references,cache-misses,L1-dcache-loads,L1-dcache-load-misses \
  -- sleep 300
```

I used this in order to figure out if the regression is either from more instructions, more cache misses, more branch mispredictions, or pipeline stalls.

### 2. Sampling Profiler with Call Graphs (`perf record`)

Captures ~2.1M cycle samples across all threads over 60 seconds.

```bash
# 300s warmup, then 60s recording
sudo perf record -g -p $PID -o perf.data -- sleep 60
```

The recorded data was converted to folded stacks and flamegraphs

The folded stack format encodes cycle counts per call stack:
```
process;func1;func2;leafFunc 1234567890
```

This lets us compute self cycles (time spent in a function itself, not its callees) and inclusive cycles (time including callees) for every function, and the difference between the before and after for the PR.

---

## Hardware Counter Results (SET, 96-byte)

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,523.6B | 1,648.6B | +8.2% |
| Instructions | 6,011.7B | 6,505.7B | +8.2% |
| IPC | 3.95 | 3.95 | 0% |
| Branch misses | 620.3M | 684.3M | +10.3% |
| L1 dcache miss rate | 0.45% | 0.43% | 0% |


The IPC (instructions per cycle) before and after the PR is essentially identical at ~3.95. If the regression were dominated by branch mispredictions or memory stalls (e.g., cache misses), IPC would typically decrease because the CPU would spend more cycles stalled waiting for data or recovering from pipeline flushes. Instead, both cycles and instructions increased proportionally while IPC remained unchanged. This indicates that the CPU pipeline is operating at similar efficiency and the regression is not caused by additional waiting. Rather, the workload is executing more instructions per request, meaning the system is performing additional work rather than spending more time stalled.

Branch misses grew +10.3%, roughly in proportion to the +8.2% instruction growth, indicating that the misprediction rate did not meaningfully change — the extra branch misses are simply from executing more branches, not from harder-to-predict branches.

---

## Sampling Profiler Results (SET, 96-byte)

Total cycles captured across all threads:

| | Before | After | Delta |
|---|---|---|---|
| All threads | 1,376.3B | 1,375.7B | −0.04% |
| Main thread | 160.7B | 156.1B | −2.9% |
| IO threads | 1,215.6B | 1,219.6B | +0.3% |

The regression reflects an increase in work per request since each request now requires more instructions and cycles, so fewer requests complete in the same time.

Profiling shows that execution time shifted across functions since approximately 28.1B cycles moved from previously hot functions to newly hot ones. This indicates the regression is caused by a change in execution paths rather than increased stalls or idle time.

#### Functions that got MORE expensive

| Delta | Before | After | Function | Thread | Explanation |
|---|---|---|---|---|---|
| +4.78B | 999.7B | 1004.5B | IOThreadMain | IO | IO threads spin-wait longer due to slower main thread |
| +4.00B | 0.0B | 4.0B | **objectGetVal** | Both | New accessor used across ~2,200 call sites |
| +3.77B | 11.2B | 15.0B | IOThreadFreeArgv | IO | Free path now calls decrRefCount → objectGetVal |
| +3.79B | 0.9B | 4.7B | sdsfreeVoid | IO | More SDS frees from embedded object teardown |
| +2.23B | 0.2B | 2.5B | sdsHdrSize | Both (1.76B main) | Called by objectGetVal embedded path |
| +1.98B | 0.0B | 2.0B | **objectSetVal** | Main | New function — every value write goes through this |
| +1.58B | 0.0B | 1.6B | createEmbeddedStringObject... | IO | New function — replaces createStringObject |
| +0.85B | 0.0B | 0.9B | decrRefCount.part.0 | Both | Compiler-generated cold path containing inlined objectGetVal |

#### Functions that got LESS expensive

| Delta | Before | After | Function | Explanation |
|---|---|---|---|---|
| −4.46B | 4.5B | 0.0B | sdsfreeVoid (main) | Moved to IO threads |
| −3.73B | 16.0B | 12.3B | zmalloc_used_memory | Less work due to shifted allocation paths |
| −2.38B | 4.0B | 1.6B | decrRefCount | Logic split into decrRefCount + decrRefCount.part.0 |
| −1.38B | 4.4B | 3.0B | dbSetValue | Cycles now attributed to objectGetVal/objectSetVal |
| −0.80B | 1.6B | 0.8B | createStringObject | Replaced by createEmbeddedStringObjectWithKeyAndExpire |

### objectGetVal Callers (SET workload)

The 5.76B inclusive cycles in objectGetVal (self time + callees like sdsHdrSize) break down by caller:

| Cycles | Caller | What it does |
|---|---|---|
| 1.34B | setKey | Setting the key's value in the DB |
| 1.21B | dbSetValue | Updating an existing key's value |
| 0.68B | addReply | Constructing the reply to send back |
| 0.67B | lookupKeyWrite | Looking up the key for writing |
| 0.47B | removeExpire | Removing expiry from the old key |
| 0.43B | setGenericCommand | The SET command implementation |
| 0.27B | addCommandToBatchAndProcessIfFull | Batching commands for IO threads |
| 0.69B | (others) | Spread across 7+ other functions |

---

## Regression Analysis (SET, 96-byte)

The main thread executes the command pipeline. The regression shows up as fewer completed requests in the same time, which implies higher work per request.

The profile and hardware-counter data suggest that the increase is not dominated by branch or cache stalls. Instead, the system appears to be doing more instructions per request, with much of the extra cost coming from the new accessor/helper layer introduced by the PR.

Contributors include:

- **objectGetVal** is now on many hot SET-path call sites (setKey, dbSetValue, lookupKeyWrite, addReply, removeExpire, etc.). Even a small per-call overhead becomes significant when multiplied across the full command pipeline.

- **objectSetVal** adds measurable cost to write/update paths.

- **objectGetKey** and related helpers now contribute additional overhead on key access/comparison paths.

- **sdsHdrSize** appears in the profile due to the new embedded-object traversal paths.

- **createEmbeddedStringObjectWithKeyAndExpire** replaces simpler creation paths with more complex logic.

- **IO-thread free/unref paths** now pick up additional helper/accessor costs, which becomes visible under pipelined workloads.

- **IO-thread top-level overhead** also rises slightly, consistent with changed batching/scheduling behavior under backpressure.

Overall, the regression seems to be from the cumulative cost of the abstraction layer. Direct field access that was previously a single load is now routed through helper functions and additional checks across many hot call sites. The CPU is simply executing more instructions to do the same logical operation, and at high throughput where the CPU is the bottleneck, we will see fewer requests per second.

---

## Hardware Counter Results (GET, 16-byte)

Collected with the same methodology as SET (300s warmup, 300s collection on the main thread under sustained load).

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,121.1B | 1,202.6B | +7.3% |
| Instructions | 4,497.7B | 4,834.3B | +7.5% |
| IPC | 4.01 | 4.02 | ~0% |
| Branch misses | 341.1M | 372.2M | +9.1% |
| L1 dcache miss rate | 0.42% | 0.40% | ~0% |

The same pattern as SET 96-byte: cycles and instructions grew proportionally (+7.3% and +7.5%), IPC stayed flat at ~4.0, and cache miss rates are unchanged. The regression is purely from executing more instructions per request — not from stalls, mispredictions, or cache pressure.

Notably, the IPC for GET (4.01–4.02) is slightly higher than SET (3.95), and this is consistent with GET being a simpler read-only pipeline with better instruction-level parallelism.

---

## Sampling Profiler Results (GET, 16-byte)

A separate profiling run was performed for GET 16-byte values using the same methodology (300s warmup, 60s recording with call graphs). This workload hits a fundamentally different code path than SET 96-byte since 16-byte values are embedded (`hasembval=1`), so objectGetVal takes the "slow path" every time.

Total cycles captured across all threads:

| | Before | After | Delta |
|---|---|---|---|
| All threads | 1,139.9B | 1,182.3B | +3.7% |
| Main thread | 126.8B | 131.4B | +3.6% |
| IO threads | 1,013.1B | 1,051.0B | +3.7% |

GET 16-byte shows a +3.7% increase in total sampled cycles across all threads, alongside fewer completed requests. The extra work per request both consumed more aggregate CPU time and reduced throughput.

#### Functions that got MORE expensive (GET 16-byte)

| Delta | Before | After | Function | Thread | Explanation |
|---|---|---|---|---|---|
| +35.35B | 804.1B | 839.5B | IOThreadMain | IO | IO threads spin longer due to slower main thread |
| +4.62B | 0.0B | 4.6B | **objectGetVal** | Main (4.3B) | New function — takes embedded slow path for every 16-byte value |
| +1.88B | 0.0B | 1.9B | createEmbeddedStringObject... | IO | New function — replaces createStringObject |
| +1.75B | 0.95B | 2.7B | **stringObjectLen** | Main | +185% — now calls objectGetVal → sdslen → sdsHdrSize |
| +1.09B | 0.25B | 1.3B | **sdsHdrSize** | Both | +431% — called from objectGetVal's embedded path |
| +0.80B | 0.0B | 0.8B | decrRefCount.part.0 | Both | Compiler-generated cold path with inlined objectGetVal |
| +0.72B | 2.3B | 3.0B | siphash | Main | +31.8% — hashing now goes through objectGetKey |
| +0.55B | 2.1B | 2.6B | hashtableFind | Main | +26.2% |
| +0.54B | 1.8B | 2.4B | objectGetKey | Main | +29.8% — key access through new accessor |

#### Functions that got LESS expensive (GET 16-byte)

| Delta | Before | After | Function | Explanation |
|---|---|---|---|---|
| −1.11B | 8.2B | 7.1B | tryOffloadFreeArgvToIOThreads | Less overhead in offload path |
| −1.02B | 1.6B | 0.6B | createStringObject | Replaced by createEmbeddedStringObject... |
| −0.79B | 2.2B | 1.4B | decrRefCount | Logic split into decrRefCount + decrRefCount.part.0 |
| −0.70B | 6.5B | 5.8B | IOThreadFreeArgv | −10.9% |
| −0.70B | 2.2B | 1.5B | lookupKeyReadOrReply | Cycles attributed to objectGetVal instead |
| −0.56B | 5.7B | 5.2B | _addReplyToBufferOrList | −9.8% |
| −0.53B | 1.2B | 0.7B | _addReplyLongLongWithPrefix | −44.7% |

### The GET 16-byte Embedded Path Problem

The main difference from SET 96-byte is that every objectGetVal call takes the slow path. For SET with 96-byte values, `hasembval=0` and objectGetVal returns `o->val_ptr` directly. For GET with 16-byte values, `hasembval=1` and objectGetVal must:

1. Test `hasembval` bit → branch taken to embedded path
2. Compute `objectEmbeddedData(o)` — pointer to data after the header
3. Check `hasexpire` → conditionally skip 8 bytes
4. Check `hasembkey` → if set, read `hdr_size` byte, advance by `1 + hdr_size`, call `sdslen()` on the key data, advance by `sdslen + 1` (null terminator)
5. `assert(o->encoding == OBJ_ENCODING_EMBSTR)`
6. Call `sdsHdrSize(SDS_TYPE_8)` to determine the SDS header size
7. Return `data + sdsHdrSize(SDS_TYPE_8)`

The full function compiles to ~67 instructions on aarch64 (both paths combined). A typical slow-path execution (embedded value with key, no expire) executes ~30 instructions within objectGetVal itself, plus a function call to `sdsHdrSize`.

The stringObjectLen regression (+185%) is the clearest signal of this. On every GET, the server calls `stringObjectLen(o)` to format the bulk reply prefix (`$16\r\n`). Before the PR, this was `sdslen(o->ptr)` but after the PR, it's `sdslen(objectGetVal(o))`, which must traverse the entire embedded layout first. This single function went from 0.95B to 2.70B cycles.

The GET pipeline calls objectGetVal fewer times per request than SET, but each call is significantly more expensive because of the embedded traversal (~30 instructions vs ~4 for the fast path). The net result is a similar ~6% regression.

---

## What I Tried to Fix The Regression

### Attempt 1: Reduce calls to objectGetVal

**Reasoning**: I initially assumed objectGetVal() was the reason for the regression so I reduced the number of times it was called throughout the codebase.

**Result** No measurable improvement.

### Attempt 2: `static inline` objectGetVal

**Reasoning**: Moving objectGetVal's definition to `server.h` as `static inline` would let the compiler inline it at all call sites.

**Result**: No measurable improvement. Inlining saves ~2 instructions per call (`bl` + `ret`) but can bloat the binary and potentially cause instruction cache pressure that offsets any savings.

### Attempt 3: Targeted decrRefCount + likely() Fix

**Reasoning**: The biggest single contributor is IOThreadFreeArgv. I wanted to bypass objectGetVal in decrRefCount's NULL check by directly testing `o->hasembval || o->val_ptr != NULL`. I also added `likely(!o->hasembval)` to objectGetVal to help branch prediction.

**Result**: No measurable improvement. All benchmark results within noise (overlapping 99% confidence intervals).

---

## Conclusions

The regression looks architectural and it's the inherent cost of replacing a direct pointer dereference with an accessor function. To recover the full 6%, we'd need to make objectGetVal essentially free (0 extra instructions vs the old `o->ptr`) and reduce the cost of objectSetVal, createEmbeddedStringObjectWithKeyAndExpire() method, and the IO thread free path. The PR trades CPU cycles for memory. At high throughput (1600 clients, pipeline 10, 9 IO threads), the CPU has no slack and the extra instructions directly reduce throughput. At normal workloads, the regression is invisible because the CPU is not the bottleneck.

---