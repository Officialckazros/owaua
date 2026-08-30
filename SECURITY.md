# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security vulnerability or a possible exposure of private Discord data. Use [GitHub's private security advisory form](https://github.com/Officialckazros/owaua/security/advisories/new) and include:

- What is affected and the version or commit you tested.
- Clear reproduction steps or a minimal proof of concept.
- The impact you observed or expect.
- Any suggested mitigation, if you have one.

If the advisory form is unavailable, contact the maintainer through the contact route listed on the project's GitHub profile. Do not include secrets, Discord tokens, private message contents, or user data in a public report.

## Supported code

Security fixes are prioritized for the current `main` branch and the most recent tagged release. This project is self-hosted, so operators are responsible for updating their own deployments and rotating their own credentials.

## Scope

Useful reports include authorization bypasses, cross-guild or cross-user data access, consent or deletion failures, unsafe handling of secrets, server-side request forgery, command/action escalation, and dashboard authentication or CSRF flaws.

Please give maintainers a reasonable chance to investigate and release a fix before disclosing details publicly.
