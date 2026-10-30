# Architecture

Work in progress. The full design (the diagram, services, data flow and one section
for each requirement) comes later.

## Resilience and error handling

### Observed: primary model outage (2026-10-30)

During the first setup check (`da-assistant check`), the primary model did not answer.
The same API key worked with the fallback model, and BigQuery was fine.

| Attempt | Model | Result |
|---|---|---|
| 1 | `gemini-3.8-flash` (primary) | 503 UNAVAILABLE: "This model is currently experiencing high demand. Spikes in demand are usually temporary." |
| 2 | `gemini-3.8-flash` (primary) | 504 DEADLINE_EXCEEDED: "Deadline expired before operation could complete." |
| 3 | `gemini-3.8-flash` (primary) | 503 UNAVAILABLE |
| 4 | `gemini-3.7-flash` (previous Flash) | 504 DEADLINE_EXCEEDED |
| 5 | `gemini-3.5-flash-lite` (fallback) | OK in 0.7 s |

The failures repeated over several minutes, so a short retry alone would not have helped.
The previous Flash version failed too, so the outage hit the Flash tier, not one model version.

What this means for the design:

- **Retry only temporary errors.** 429, 503 and 504 are retried a few times with exponential
  backoff and jitter. 400, 401, 403 and 404 are not retried: the user gets a clear message at once.
- **Fall back to a different tier.** After the retries, the agent switches to the fallback model.
  The fallback must be a different tier (Flash-Lite), because the other Flash version was down too.
- **Remember the failure.** After repeated failures, the primary model is skipped for a few minutes
  (a simple circuit breaker), so every new turn does not wait for the same timeouts again.
- **Switch models without code changes.** The model is a setting (`DA_LLM_MODEL`). During this outage
  we kept developing on `gemini-3.5-flash-lite` by changing `.env` only. The default in code stays
  the newest Flash model.
- **Detect it fast.** The setup check showed in seconds which part failed and why,
  with the error code, the message and a hint.
