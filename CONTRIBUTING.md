# Contributing to owaua

Small, focused changes are easiest to review. Before opening a pull request:

1. Keep credentials, local databases, and server-specific settings out of Git.
2. Run `python -m unittest discover -s tests -v`.
3. Deploy with `./scripts/deploy.sh` after the tests pass so the running bot
   matches the checked-in behavior.
4. Update the README when a command or configuration setting changes.
5. Keep persona edits conversational, clear, and respectful of consent and safety.

Please explain the reason for a behavior change in the pull request description and include a test when practical.
