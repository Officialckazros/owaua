# Contributing to owaua

Thanks for taking the time to help. A small, well-explained change is more useful than a large rewrite that is hard to review.

## Before opening a pull request

1. Search existing issues and pull requests.
2. Keep each pull request to one purpose.
3. Do not add tokens, database files, Discord exports, personal data, or real server IDs.
4. Preserve the privacy and permission boundaries already in the project. In particular, do not make message retention, moderation, or Discord mutations more permissive by accident.

## Local checks

Use Python 3.12–3.14, create a virtual environment, and install the locked dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
OWAUA_LOAD_DOTENV=0 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
OWAUA_LOAD_DOTENV=0 PYTHONPATH=src .venv/bin/python scripts/run_ai_evals.py
OWAUA_LOAD_DOTENV=0 PYTHONPATH=src .venv/bin/python scripts/generate_slash_reference.py --check
.venv/bin/ruff check src tests desktoppet/pet.py desktoppet/tests
```

If you change the Cloudflare Worker, also run its tests from `cloudflare-worker/`:

```bash
npm ci --ignore-scripts
npm test
npm run dry-run
```

## Pull requests

Explain the user-visible change, the privacy or security impact, and the checks you ran. Update documentation or tests when behavior changes. Please avoid unrelated formatting churn.

Changes to legal copy, retention, consent, moderation, blocking, or action permissions deserve extra context because they affect real people and servers.

You retain copyright in work you submit. Only submit work you have the right to
license. By intentionally submitting a contribution, you agree that accepted
changes may be distributed under the contribution terms in the
[Obsidian License 1.0](LICENSE).
