# contracts/jev/v1 — behaviour shared by the Android and Windows apps

The two apps are separate codebases (Kotlin / Python) that must behave the same at the trust
boundaries. The files here are language-neutral test vectors; **each repo's own tests read them**.

| file | what it pins |
|---|---|
| `origin.json` | what an "origin" is, and when two URLs are the same origin (a key may only go to its own origin) |
| `http_hint.json` | the fixed local text shown for an HTTP status (provider error bodies are never shown) |
| `reply_parse.json` | how a bilingual model reply is parsed, and the fixed failure messages |
| `fill_verdict.json` | may a reply be written into what is on screen now (divergences kept on purpose are marked with their platform reason) |
| `credential_binding.json` | the four binding states of a stored key and the one outcome of each |
| `fill_support.json` | which app fills how today (fresh-verified / copy-only; production is the truth, the file describes it) |
| `capability.json` | can a route's model take images: four states + evidence, per-provider model-list adapters, the capability cache key, and what a request does with an image per state |
| `retry_policy.json` | the seven failure classes (auth / rate_limited / timeout / unsupported / invalid_response / cancelled / transport), which are retried and how often, and that a stale generation never retries |
| `reply_outcome.json` | one reply round trip from the provider's message envelope to the parsed result or one fixed failure (refusal, malformed, fewer than three, analysis never a reply) |

## Rules
- The canonical copy lives in the Android repo (`contracts/jev/v1/`); the Windows repo carries a
  byte-identical mirror. `MANIFEST.sha256` lists the hash of every other file; both repos' tests check
  their files against it, and Windows CI compares its manifest with the Android repo's.
- Change a vector = change it in both repos in the same round, regenerate the manifest
  (`cd contracts/jev/v1 && sha256sum README.md *.json > MANIFEST.sha256`), and make both test suites pass.
- Breaking a vector on purpose means a new directory (`v2`), not an edit of `v1`.
- Vectors contain synthetic data only. No real chats, no keys.
