# Security policy

## Reporting a vulnerability

Please report security problems privately, using GitHub Security Advisories (private vulnerability reporting) of this
repository: open the **Security** tab and choose **Report a vulnerability**. Do not open a public issue or a pull request
for a vulnerability, and do not send reports to personal e-mail addresses.

Include what you found, how to reproduce it, the version or commit, and what an attacker could do with it.

## What to expect

We handle reports by coordinated disclosure, on a best-effort basis. This is a small project: we do not promise a
response time or a fix date. We will read the report, tell you whether we consider it a vulnerability, and work with you
on a fix and on when to publish the advisory. Please give us a reasonable time to fix before you disclose.

## Scope

In scope:

- the panel (`hub/painel.py`, `hub/ui.py`): authentication bypass, CSRF, injection, cross-site scripting;
- the secrets vault and its key handling (`hub/comum/`, `hub/segredos_fonte.py`);
- login and the first-run assistant (`/comecar`), including who becomes the owner of a fresh install (it requires the one-time setup token shown only in the terminal and the container log);
- the Docker deploy (`Dockerfile`, `compose.yml`, `deploy/`) when it exposes more than documented.

Out of scope: problems that need an attacker who already controls the server, the Docker socket (it is off by default and
documented as root-equivalent when turned on), or the owner's own account; reports about third-party services.

## Secrets

Secrets never go in `krohnos.toml`. The toml holds names and settings; values live in the vault and in the Docker volumes.
If you find a secret value in the repository or in its history, report it as a vulnerability.
