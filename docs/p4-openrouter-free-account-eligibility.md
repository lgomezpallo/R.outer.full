# P4 — OpenRouter free-model eligibility (offline, no production activation)

**Purpose:** distinguish **zero advertised token cost** from **explicitly usable without purchasing credits**. A zero-priced non-`:free` ID can still require a funded account. This is a conservative routing policy, not proof of any user's account status.

Source checked 2026-11-01: [OpenRouter free models](https://openrouter.ai/collections/free-models) states that `:free` variants do not require a credit purchase, whereas other zero-priced models can require a funded account. [OpenRouter free pricing](https://openrouter.ai/pricing) describes 50 requests/day for the free tier (also 20 requests/minute in the free-model collection).

## Selection rule

A model is classified `free` from OpenRouter inventory **only when**:
- The ID ends with `:free` or is exactly `openrouter/free`.
- The inventory contains explicit numeric zero prompt and completion prices.
- Every advertised optional charge is numeric zero, and no unrecognized pricing field is present.
- No duplicate row for that ID reports a paid or unknown price.

Non-`:free` zero-priced models are `unknown` (not routed). Suffix alone **never** overrides paid, unknown, null or missing prices. Paid and unknown models remain visible in diagnostics but are ineligible for inference.

## Real public model IDs exercised with mock inventory (not real API calls)

- `google/gemma-4-26b-a4b-it:free` — [official model page](https://openrouter.ai/google/gemma-4-26b-a4b-it%3Afree).
- `nvidia/nemotron-3-ultra-550b-a55b:free` — [official model page](https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b%3Afree).
- `openrouter/free` — [official router page](https://openrouter.ai/openrouter/free/apps), selects an eligible free variant automatically; explicit model IDs are preferable for per-model cooldown and fallback.

Tests use `httpx.MockTransport` and a local fake completion client, with **no credentials, network, billing changes or production deployment**. Live availability and the account's actual billing settings are **not** established. A fresh authenticated inventory and operator authorization would still be needed before enabling live inference.
