# Inference load tuning

[Русский](tuning.md) · English

Measured on an RTX 3070 Ti Laptop (8 GB, Ampere, 150 W TGP), vLLM 0.23,
Qwen2.5-7B-Instruct-AWQ. Request profile: 256 tokens in and 256 out,
`--ignore-eos`, seed=0.

## Throughput under load

| Concurrent requests | Throughput, tok/s | Zone |
|---:|---:|---|
| 1  | 47  | linear |
| 8  | 323 | linear |
| 16 | 543 | knee |
| 32 | 815 | peak |
| 64 | 722 ↓ | collapse |

The best point is **32** concurrent requests. At 64 throughput drops: memory for
the KV cache runs out, and the engine starts evicting requests (preemption).

## Levers

- **fp8 KV cache** — 1 byte per value instead of 2, twice the cache capacity. It
  does not move the peak (the limit is compute, the GPU is already at 100%), but
  it prevents collapse under overload.
- **CUDA graphs (PIECEWISE mode)** — +18% to the peak, −18% to time per token.
  Only "light" graphs fit in 8 GB (~62 MiB vs ~1.4 GiB for FULL mode).

With both levers — **peak 962 tok/s** (815 without them), ~25 ms per token. The
setup in `docker-compose.yml` runs without the levers (`--enforce-eager`, KV
cache without fp8); the quality scores were measured on it.

## Inference cost

The dashboard computes it on the fly from GPU power and generation speed:

```
₽/1M = 2.831 × P_gpu / S
```

where `P_gpu` is GPU power (W) and `S` is throughput (tok/s). The constant 2.831
is the ₽7.28/kWh tariff × a node overhead factor k≈1.4 (CPU, memory, fans, PSU
losses — an estimate, to be refined with a wattmeter) and the conversion
J → kWh → 1M tokens. At the peak — **₽0.44 per 1M tokens**: electricity only,
at full load, excluding hardware and idle time (the cost of an answer is in
[pilot.en.md](pilot.en.md)).

## Lesson: n=1 ≠ a finding

In one run throughput "collapsed". After a clean restart the collapse did not
reproduce: it was a one-off engine hang, fixed by restarting the container.
Recording "fp8 breaks vLLM" from a single measurement would have been an error
in the report. Reproduce before concluding.
