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
  backoff and jitter. 400, 401 and 403 are not retried: the user gets a clear message at once.
  404 (model not found) is not retried either, but the fallback model takes over.
- **Fall back to a different tier.** After the retries, the agent switches to the fallback model.
  The fallback must be a different tier (Flash-Lite), because the other Flash version was down too.
- **Remember the failure.** After repeated failures, the primary model is skipped for a few minutes
  (a simple circuit breaker), so every new turn does not wait for the same timeouts again.
- **Switch models without code changes.** The model is a setting (`DA_LLM_MODEL`). During this outage
  we kept developing on `gemini-3.5-flash-lite` by changing `.env` only. The default in code stays
  the newest Flash model.
- **Detect it fast.** The setup check showed in seconds which part failed and why,
  with the error code, the message and a hint.

### What the agent does now

A later measurement during the same outage changed one detail: a single attempt failed only
after 21 to 58 seconds (see "How the timing was measured" below). Retrying a model that fails
this slowly mostly makes the user wait. So:

- Temporary errors are retried only while less than 10 seconds have passed since the first
  attempt (`DA_LLM_RETRY_WINDOW_S`). A quick 429 gets a retry; a slow 503 goes straight to
  the fallback model.
- Every attempt has a timeout of 30 seconds (`DA_LLM_TIMEOUT_S`). The guard's call is tiny,
  and the guard lets a message through to the data rules when its model fails, so it waits
  only 10 seconds (`DA_GUARD_TIMEOUT_S`). This came from a trace: one guard call waited the
  full 30 seconds for a slow model.
- After the primary model fails, it rests for 5 minutes (`DA_LLM_COOLDOWN_S`): calls go
  to the fallback model at once.
- The fallback model takes over in the middle of a turn: it accepts the tool calls that the
  primary model made earlier in the turn (checked on 2026-10-30).
- BigQuery calls use the retries built into the BigQuery client, which retries rate limits
  and backend errors. On top of that, every job has a cap on bytes billed and a timeout.
- If both models fail, the user gets one clear message, and the session goes on.

Live run with the primary model still down: the first question fell back after one 503 and
was answered in 32 s; the second question skipped the primary model and took 18 s.
Both answers were correct, and the trace shows each step (`/trace`).

### How the timing was measured

The time to a failure only means something if it is one attempt, not several retries inside
the SDK. So each call went to `gemini-3.8-flash` through LangChain's `ChatGoogleGenerativeAI`
with `max_retries=1`. LangChain passes this to the google-genai SDK as
`HttpRetryOptions(attempts=1)`, which the SDK documents as "including the original request;
0 or 1 means no retries". The SDK's other, implicit retries cover only lost connections in
async code, and they are off when retry options are set. The client timeout was 60 seconds.
To be sure, we counted the HTTP requests of each call by wrapping `httpx.Client.send`.

| When (2026-10-30) | SDK setting | HTTP requests | Result | Time to the error |
|---|---|---|---|---|
| First measurement | `max_retries=1` | 1 | 503 UNAVAILABLE | 56.5 s |
| First measurement | `max_retries=1` | 1 | 503 UNAVAILABLE | 23.1 s |
| Check, 17:05 CET | `max_retries=1` | 1 | 503 UNAVAILABLE | 20.8 s |
| Check, 17:05 CET | `max_retries=1` | 1 | 504 DEADLINE_EXCEEDED | 58.1 s |
| Check, 17:05 CET | `max_retries=3` | 3 (58.6 s, 58.8 s, 58.6 s) | 504 DEADLINE_EXCEEDED | 180.3 s |

The last row is the setting the agent had before: three SDK attempts made the user wait three
minutes for one error. Now the SDK makes one attempt, our own retry layer decides what happens
next, and every attempt shows in the traces. With the 30-second timeout, a slow failure costs
at most 30 seconds before the fallback model answers.

## Where data lives

### In the prototype

| Data | Where | How long | How it is removed |
|---|---|---|---|
| Raw data (orders, users, products) | BigQuery public dataset, read-only | - | - |
| Direct personal data (names, emails, addresses, coordinates) | Never leaves BigQuery: the safe views do not select it | - | - |
| The conversation: questions, answers, query results | In memory (LangGraph `InMemorySaver`), one CLI session | Until the session ends | Gone with the process |
| Saved reports | SQLite, `data/assistant.db` | Until the author deletes them | Hard delete. `secure_delete` overwrites the deleted text in the file (tested) |
| Pending deletes | SQLite: report ids, expiry, status | Kept, with their status | - |
| Audit log | SQLite: who, when, action, report ids, count | Kept | Holds no titles and no report text |
| Traces | `data/traces.jsonl` | Kept | Hold no question, answer or report text, so nothing to remove |

### Traces keep ids, hashes, sizes and timings, never text

We chose this over "text with a short retention, deleted together with the report":

- **A hard delete must be provable.** If traces never hold report or answer text, deleting a
  report leaves nothing behind. The traces need no deletion logic, no link to the reports
  and no cleanup job that can fail.
- **Most failures show without the text.** A trace has every step: which one failed, the
  error code, the SQL, rows, bytes, retries, the fallback, the guard verdict, hidden groups
  and masked values. That is enough to tell what went wrong.
- **A fingerprint replaces the text.** A question or an answer is stored as the first 16 hex
  characters of its SHA-256 and its length. It shows repeated questions, and it checks a text
  that a user shows you, but it does not hold the text. A short question can be guessed by
  hashing candidates; in production the fingerprint is an HMAC with a secret key.
- **What stays as text:** the SQL as the model wrote it and the short purpose it gives each
  query, both with the personal data patterns masked. Neither is answer or report text. String
  values in the SQL (for example `'Brasil'`) stay, because they explain empty results. The SQL
  that ran is not stored: it starts with the safe views, so a cut would lose the model's own
  WHERE and GROUP BY. It follows from the model's SQL and the user's brand scope, which the
  turn record keeps, and its fingerprint shows which rewrite ran.
- **The trade-off:** a trace does not show the exact wording. In a live session, `/trace`
  shows the steps and the user sees the conversation. In production, a per-incident debug
  capture, with the user's consent and a short retention, would add the text when needed.

### In production

- **Conversations:** a LangGraph checkpointer on Firestore instead of memory, with a time to
  live (for example 30 days), so a conversation does not outlive its use.
- **Reports, pending deletes, preferences:** Firestore, every read and delete filtered by
  the author on the server.
- **Traces and metrics:** OpenTelemetry spans to Cloud Trace, metrics to Cloud Monitoring, logs
  to Cloud Logging, all with a set retention. The same rule holds: no text in spans or logs.
- **Backups:** point-in-time recovery and scheduled backups keep old copies, so a deleted
  report can live on in a backup until the backup expires. Two ways to keep "no recovery":
  1. State it plainly: "deleted from all backups within N days", where N is the longest
     backup retention.
  2. Crypto-shredding: encrypt each report with its own data key, kept in a separate store
     that has no long backups. Deleting the key makes every copy of the report unreadable at
     once, backups included. This is the one that keeps "no recovery" strict, and the one we
     would choose.


## Known limits

### Brands with small volumes: most fine cuts are hidden

Groups with fewer than 5 customers are hidden (k = 5). For a brand manager with a small
volume, fine cuts are often hidden completely. For Alice (Calvin Klein and Diesel), US state
by product category in September 2026 had 29 groups, and all 29 had fewer than 5 customers.

Self-correction must not inflate costs here, and at first it did: the agent tried ever
coarser groupings until it had used its whole budget. Now, after a result with every group
hidden, the model gets one more query (a coarser grouping), whatever it shows. Then it must
answer with what can be shown and suggest a coarser cut, for example by state only or by
quarter. Further queries in the same step are not run at all, so they cost nothing.

| The same question (eval case `small-groups-stop-early`) | Model calls | Queries | Cost | Time |
|---|---|---|---|---|
| Before the limit | 8 (the whole budget) | 6 | $0.0109 | 25 s |
| With the limit | 6 | 3 | $0.0068 | 19 s |

The rule only protects privacy and cost; it does not make small brands' data more useful.
What production can add: check the group sizes of a few candidate cuts in one query and offer
the finest cut that shows data; combine small groups into an "Other" row; or answer with
BigQuery differential privacy, which adds noise to small groups instead of hiding them.
