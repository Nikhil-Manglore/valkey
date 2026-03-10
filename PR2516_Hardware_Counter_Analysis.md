# PR #2516 Hardware Counter Results

All data collected on **c7g.metal** (AWS Graviton3, aarch64, 64 vCPUs).

Config: 1600 clients, pipeline 10, 9 IO threads.
Method: 300s warmup, 300s `perf stat` attached to the server process under sustained load (used valkey benchmark).

---

## Summary

I calculated these numbers for Before and After the PR was merged.

| Combo | Cycles | Instructions | IPC Before | IPC After | Cache Miss Rate |
|---|---|---|---|---|---|
| GET 96B | **−3.6%** | **−4.4%** | 3.97 | 3.94 | 0.43% → 0.45% |
| SET 96B | **+8.2%** | **+8.2%** | 3.95 | 3.95 | 0.45% → 0.43% |
| GET 16B | **+7.3%** | **+7.5%** | 4.01 | 4.02 | 0.42% → 0.40% |
| SET 16B | **+12.1%** | **+12.7%** | 4.14 | 4.17 | 0.32% → 0.29% |

IPC is flat across all combos. Cache miss rates either improved or are unchanged. The throughput regression we noticed is due to more instructions per request.

### Regression Data (valkey-perf-benchmark)

Data from Valkey-Perf-Benchmark: [Dashboard Link](https://perf-dashboard.valkey.io/public-dashboards/3e45bf8ded3043edaa941331cd1a94e2?from=2026-01-05T19:00:00.000Z&to=2026-01-06T06:59:58.000Z&timezone=UTC)

| Combo | Throughput Delta | Instructions Delta |
|-------|-----------------|-----------------------|
| GET 96B | **-0.249%** | -4.4% |
| GET 16B | **-6.27%** | +7.5% |
| SET 96B | **-5.91%** | +8.2% |
| SET 16B | **-2.72%** | +12.7% |

GET 96B is near-zero, consistent with our finding that it's the only combo where instructions decreased. GET 16B is the worst GET regression (-6.27%) and it is consistent with the embedded slow-path overhead. SET 96B shows -5.91% which aligns with its +8.2% instruction increase.

SET 16B is an outlier since it has the largest instruction increase (+12.7%) but a mild throughput drop (-2.72%). This could reflect noise in valkey-perf-benchmark or my benchmarking.

---

## GET 96-byte (no regression)

96-byte values are not embedded (`hasembval=0`) so objectGetVal takes the fast path (direct pointer dereference).

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,144,485,975,791 | 1,103,556,109,091 | −3.6% |
| Instructions | 4,543,570,979,961 | 4,343,151,553,278 | −4.4% |
| IPC | 3.97 | 3.94 | −0.8% |
| Branch misses | 360,059,061 | 388,068,151 | +7.8% |
| L1 dcache miss rate | 0.43% | 0.45% | +0.02% |

---

## SET 96-byte

96-byte values are NOT embedded (`hasembval=0`). objectGetVal takes the fast path, but it is called multiple times per SET (key lookup, value swap in dbSetValue, objectSetKeyAndExpire). The overhead comes from function call indirection through objectGetVal/objectSetVal where previously we used direct `o->ptr` accesses.

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,523,624,688,856 | 1,648,569,219,915 | +8.2% |
| Instructions | 6,011,695,913,601 | 6,505,739,094,262 | +8.2% |
| IPC | 3.95 | 3.95 | 0% |
| Branch misses | 620,304,741 | 684,286,609 | +10.3% |
| L1 dcache miss rate | 0.45% | 0.43% | −0.02% |

---

## GET 16-byte

16-byte values are embedded (`hasembval=1`). objectGetVal takes the slow path (~30 instructions) on every call.

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,121,083,136,347 | 1,202,582,871,466 | +7.3% |
| Instructions | 4,497,651,296,351 | 4,834,345,620,453 | +7.5% |
| IPC | 4.01 | 4.02 | ~0% |
| Branch misses | 341,083,996 | 372,155,574 | +9.1% |
| L1 dcache miss rate | 0.42% | 0.40% | −0.02% |

---

## SET 16-byte (worst regression)

16-byte values ARE embedded (`hasembval=1`). objectGetVal is called multiple times per SET (key lookup, value swap in dbSetValue, objectSetKeyAndExpire) and each call takes the slow path (~30 instructions) instead of the fast path. This is the same function call indirection overhead as SET 96B, but amplified because every objectGetVal call walks the embedded data layout instead of returning a direct pointer.

### Iteration 1

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,558,198,036,471 | 1,779,521,591,379 | +14.2% |
| Instructions | 6,472,176,032,983 | 7,393,807,947,459 | +14.2% |
| IPC | 4.15 | 4.15 | 0% |
| Branch misses | 411,009,295 | 535,218,879 | +30.2% |
| L1 dcache miss rate | 0.32% | 0.29% | −0.03% |

### Iteration 2

| Metric | Before | After | Delta |
|---|---|---|---|
| Cycles | 1,571,509,803,043 | 1,728,040,656,201 | +10.0% |
| Instructions | 6,491,048,477,429 | 7,215,526,961,653 | +11.2% |
| IPC | 4.13 | 4.18 | +1.2% |
| Branch misses | 411,009,295 | 527,566,215 | +28.3% |
| L1 dcache miss rate | 0.32% | 0.29% | −0.03% |

### Average

| Metric | Before (avg) | After (avg) | Delta |
|---|---|---|---|
| Cycles | 1,564,853,919,757 | 1,753,781,123,790 | +12.1% |
| Instructions | 6,481,612,755,206 | 7,304,667,454,556 | +12.7% |

---

# Sampling Profiler (perf record) Results

All data collected with `perf record -g` for 60 seconds under sustained load (same config as above). Both `perf stat` and `perf record` attach to the entire server process (all threads). IO threads dominate all profiles (68-74% of samples).

---

## objectGetVal Disassembly (SET 16B, AFTER)

From `perf annotate -s objectGetVal` on the SET 16B AFTER run (8566 samples). Comments after `;` are added annotations:

```
  3.61% :   ldr     x1, [x0]              ; load object flags
 36.64% :   tbz     x1, #34, fast_path    ; branch on hasembval (36.64% of samples here)
 11.29% :   tst     x1, #0x100000000      ; check hasembval for data offset
  1.47% :   add     x2, x0, #0x8          ; compute data ptr (embedded, val_ptr reclaimed)
  3.85% :   add     x0, x0, #0x10         ; compute data ptr (non-embedded, val_ptr present)
  1.32% :   csel    x0, x0, x2, ne        ; select correct offset
  3.44% :   tbz     x1, #33, skip_key     ; branch on hasembkey
  0.29% :   ldrb    w2, [x0]              ; load key SDS header size
  0.85% :   add     x2, x2, #0x1          ; +1 for header size byte
  0.18% :   add     x0, x0, x2            ; advance past key SDS header
```

The `tbz x1, #34` (hasembval check) accounts for 36.64% of samples within objectGetVal and this is the hot branch that determines embedded vs non-embedded path.

---

## Conclusions

Overall the IPC is basically flat across all combinations that were tested which implies that the CPU pipeline operates at the same efficiency. The regression is from executing more instructions per request as opposed to CPU stalls or branch mispredictions. From the data profiled I saw that objectGetVal's hottest instruction is the `tbz` (test-bit-and-branch-if-zero) for `hasembval` at 36.64% of samples within the function. This is the embedded vs non-embedded decision point. From the data above we can see that GET Requests on 96-byte values is the only combo that doesn't regress, which matched up with the Valkey-Perf-Benchmark. This is because every combo pays one or both of two new costs introduced by PR #2516. These costs include embedded slow-path reads where objectGetVal is used on embedded values (~30 instructions per call to walk past expire, key header, and SDS header to find the value pointer). This only applies when `hasembval=1`. Additionally, the function call indirection through objectGetVal/objectSetVal replaces what were direct `o->ptr` accesses, adding overhead on every read and write path.

---