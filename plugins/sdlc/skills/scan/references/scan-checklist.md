# Scan checklist (validate before reporting; cite file:line; confidence = how sure the evidence is)

1. **Injection** — SQL/NoSQL built by string concatenation; shell commands with interpolated input (`exec`, `system`, `subprocess(shell=True)`, backticks); template injection; LDAP/XPath; header/log injection (CRLF).
2. **Authentication & authorization** — external-facing routes without the gateway/JWT check; object-level access (IDOR) without ownership check; role checks only in the UI; admin endpoints reachable without the admin role; `/health`-style anonymous routes doing more than health.
3. **Secrets** — credentials, tokens, private keys in code, config, CI files, notebooks, fixtures outside `evals/`; `.env` committed; default passwords.
4. **Data classification / PII** — fields tagged pii (or obviously personal: national id, card, phone, email, birth date) written to logs, error messages, analytics, or unencrypted caches.
5. **Unsafe deserialization / parsing** — `pickle`, Java native deserialization of untrusted input, `yaml.load` without SafeLoader, XML external entities, prototype pollution via `Object.assign`/merge on user JSON.
6. **File & path handling** — path traversal (`..`, absolute paths) in download/upload/receipt handlers; zip slip; world-readable temp files; unrestricted upload types/sizes.
7. **Cryptography** — homegrown crypto, ECB, static IV/salt, MD5/SHA1 for passwords, non-constant-time comparisons for MACs/tokens, insecure random for tokens.
8. **Configuration & defaults** — debug/Swagger/actuator exposed in production profiles; CORS `*` with credentials; missing TLS verification; overly broad IAM/DB grants in IaC; permissive `permissions` in CI workflows.
9. **Dependencies** — pinned versions with known-vulnerable ranges visible in manifests/lockfiles (say which manifest; recommend the CI audit tool for exact CVEs); unmaintained forks; install scripts fetching over http.
10. **Error handling & information exposure** — stack traces to clients; verbose errors leaking queries/paths; swallowed security exceptions (auth failure treated as success).
11. **Business-logic abuse** — missing rate limits on auth/payment/issuance endpoints; replay of idempotency-less operations; unbounded query parameters (limit/offset).
12. **Policy skills** — anything `config.policies` skills require (e.g. secure-api-review: gateway JWT, schema validation rejecting unknown fields, audit events with actor/action/entity/timestamp, pii never in logs).
