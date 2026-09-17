# Security hardening and verification

## Result

The implementation hardens ordinary-chat spending, settings, queue, media-fetch and retention paths. Allowlisted full mode intentionally bypasses spending and admission ceilings as described below. The earlier hardened runtime was deployed and verified on Linux; the current full-mode exemptions were subsequently verified locally. This is not a claim that the bot or its hosting environment is abuse-proof.

The initial independent source audit recorded eight findings. Existing uncommitted work, including newer YouTube validation and voice departure handling, was retained and extended. The final patch received an independent read-only bypass review; its findings about abandoned voice connections, idle retention and erasure races were addressed.

## Enforced boundaries

| Original weakness | Implemented control | Regression evidence |
| --- | --- | --- |
| Unlimited aggregate API spending | Atomic SQLite reservation before ordinary-chat provider POSTs; rolling global/user/server and lifetime attempt ceilings; durable commits; no refunds for errors; allowlisted full mode is exempt | Concurrent store instances, restart, erasure, clock rollback, duplicates, every budget dimension, storage failure |
| Persona/provider tampering | User-scoped settings; untrusted legacy global selection ignored | Per-user isolation and provider validation |
| Unbounded waiting requests | Ordinary-chat per-user and global AI admission; bounded ordinary-chat handlers and caches; no conversation wait queue; allowlisted full mode bypasses admission ceilings | Busy user across channels, global admission and cancellation cleanup |
| Unauthorized language/profile changes | Manage Server check on both set and reset paths | Permission denial and existing legitimate profile/language tests |
| Arbitrary music fetches | Ordinary guilds: canonical YouTube-video or Twitter/X-status lookup; approved-only extractors; HTTPS Googlevideo or exact Twitter video CDN streams; no download redirects; 20 MiB cap. Trusted guild `1535083112709496903` intentionally bypasses these controls. | Private/malformed/attacker hosts, redirect response, oversized stream, unsupported inputs; dedicated trusted-guild bypass tests |
| Unbounded media work | Ordinary guilds: two concurrent jobs/sessions; killable 30-second metadata worker; 50-second command deadline; bounded media buffers; forced demuxer and pipe-only FFmpeg input; Linux CPU/memory/file limits; playback deadline. The trusted music guild intentionally uses unrestricted resolution and direct FFmpeg streaming. | Resolver cancellation/reaping, format rejection, fail-before-connect, playback failure cleanup, unsupported-host refusal; trusted-guild direct-stream tests |
| Permanent conversation storage | 20 records per conversation, 10,000 total, bounded content, seven-day retention with hourly maintenance; private directory/database modes | Retention, legacy startup pruning, user isolation, erasure generation fences |
| Voice control by outsiders | Ordinary guilds require same-channel controls and do not move automatically. The trusted music guild bypasses those checks and automatic departure cleanup. | Voice-control denial, ordinary departure tests, trusted-guild persistence test |

Hangout modes are text-only. Hosted tools stay disabled in hangout. Normal responses have an 80-token cap and no image analysis. Allowlisted full mode in the trusted channel omits the output-token cap, shared API attempt ceilings, chat rate limits, admission caps, handler/input/reply truncations, and the image-analysis block, and enables native OpenAI web search plus the hosted code interpreter. Image generation is not available. Full-mode attempts are not charged against the shared ledger, so they cannot exhaust everyone else's budget. Provider errors and signed CDN request URLs are not logged. Native media subprocesses do not inherit bot/provider credentials.

See [README](README.md#abuse-controls) and [.env.example](.env.example) for configuration. Defaults are 12 global API attempts/minute, 30/user/day, 100/server/day, 200 globally/day, 1,000 lifetime and three concurrent AI requests. Those ceilings still apply to ordinary chat. `!security pause` stops future reservations, including full mode; it cannot cancel work already sent to a provider. `resume` does not reset quotas.

## Verification performed

- Current source including full-mode exemptions, Python 3.12.14: `python -m unittest discover -s tests -q` — 193 tests: **192 passed, one skipped**.
- The local skipped case is `NativeAudioTests.test_valid_wave_decodes_with_pipe_only_protocols`, requiring Linux RLIMIT support. It **passed on the production Linux server during deployment**. The production suite also ran 185 tests: 184 passed, with only the non-Linux refusal test skipped. Ubuntu CI now installs FFmpeg; CI itself has not been run from this session.
- `python -m py_compile ask.py bot.py memory.py music.py security.py music_worker.py media_exec.py` — passed.
- `bash -n scripts/run-bots.sh scripts/deploy.sh scripts/update-persona.sh` — passed.
- `git diff --check` — passed.
- `python -m pip check` — passed in the isolated verification environment.
- `python -m pip_audit -r requirements.txt` — **no known vulnerabilities found** in the Python 3.12 resolution. The original resolution flagged Pillow and PyNaCl; both were upgraded, and runtime versions were pinned. The Python 3.13-only `audioop-lts` dependency is checked by that CI matrix job.
- Discord's voice-packet encryption/decryption path was exercised locally with the upgraded PyNaCl. This does not substitute for a live Discord voice test.

Provider calls, media downloads and Discord interactions in the tests use local mocks. The native decoder test runs real FFmpeg against generated audio. Deployment verification included a real Discord gateway login, with no paid AI requests or test messages. Scan token usage was not available.

## Earlier hardened runtime deployed — 2026-09-15

This deployment verification predates the current full-mode exemptions and does not validate their live deployment.

The hardened runtime was deployed to the configured Daki server and Discord readiness was confirmed after dependency checks, a native decoder smoke test, and the production test suite passed. Runtime Python is 3.12.13, running as an unprivileged user. Effective settings were verified: DMs disabled; three concurrent AI requests; 12 attempts/minute, 30/user/day, 100/server/day, 200 globally/day and 1,000 lifetime.

The previous runtime was saved privately before deployment. The bot was stopped before uploads, uploaded files were byte-verified, and the existing credentials and persistent database were preserved. Startup now checks dependency consistency and restricts `.env` permissions. Deployment startup runs the release's explicit test modules, preventing obsolete server-only test files from being picked up. The first startup correctly failed closed on obsolete tests; the corrected startup passed and connected to Discord.

The deployment script's container-state check is only an intermediate result. A healthy deployment also requires `OWAUA_RUNTIME_VERIFIED`, `OWAUA_DEPLOY_TESTS_PASSED`, and the subsequent Discord `Logged in as` event in the console; all three were observed for this release. Real Discord voice playback has not been exercised.

## Deployment and remaining limits

1. Future deployments must include the complete runtime, including `security.py`, `music_worker.py`, `media_exec.py` and `scripts/check-runtime.py`. Preserve the pinned requirements and maintain system FFmpeg updates. Native music requires Linux; production decoder functionality was verified, but the host FFmpeg vulnerability status was not independently audited.
2. Keep `data/memory.sqlite3` persistent. Deleting it, restoring an older copy or giving each replica a different database defeats shared usage accounting. Historical spending before this change cannot be reconstructed by the new ledger.
3. These are request caps, not currency caps. Actual pricing, other applications using the same keys, compromised credentials and provider-side accounting are outside the bot's ledger. Configure appropriate provider account controls separately.
4. Run the bot under a dedicated unprivileged account/container with host CPU, memory and disk limits and restricted network egress. FFmpeg protocol restrictions and process rlimits are not a complete operating-system sandbox; an unknown native parser exploit remains a host risk. Do not give the bot Administrator permission or mount unrelated sensitive host files.
5. Local erasure does not delete Discord messages, provider-retained data, logs or backups outside this database. In-flight replies cannot restore erased rows, but a request already sent to a provider cannot be recalled.
6. Prompt filters do not prove that all generated text will be safe or appropriate. Security-sensitive permissions and budgets are enforced by Python/SQLite controls, independent of model obedience.

Pillow's fixed releases are documented in its [12.3.0 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html). The Discord maintainer describes PyNaCl 1.6 compatibility in [the dependency discussion](https://github.com/Rapptz/discord.py/discussions/10425). The pinned voice dependencies are explicit because the installed discord.py 2.7.1 voice extra still selected PyNaCl 1.5.
