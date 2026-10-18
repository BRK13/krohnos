# Contributing to Pereiraos

Thank you for the interest. Read this before opening anything.

## Pull requests from outside

Pull requests externos ainda não são aceitos até a decisão sobre CLA/DCO; abra uma issue.
(External pull requests are not accepted yet, until the CLA/DCO decision is made. Please open an issue instead:
bug reports, questions and proposals are welcome.)

## Setup

```bash
git clone https://github.com/BRK13/krohnos.git kronos && cd kronos
python3 -m unittest tests.test_neutro          # a single module
python3 -m unittest                            # the whole suite (slow; run it before a change that touches many modules)
```

Python 3 and the standard library are all you need to run the tests. Tests use no network and no production database.
To run the hub itself, follow [docs/INSTALAR.md](docs/INSTALAR.md).

## Checks

Before a change is ready, run `scripts/checar.sh` (frente 4), which arrives with the local check script.
CI: `scripts/checar.sh` rodado pelo contribuidor e pelos mantenedores (`/local-ci`). There is no hosted CI by default; the script is (frente 4).

## Rules

- **Standard library only.** No new runtime dependency without an issue that explains why.
- **HTML only through `hub/ui.py`.** Pages are not built with ad-hoc HTML documents.
- **Escape text.** Everything that comes from a user, a query string or a file is escaped before it reaches a page.
- **CSRF.** A form that changes something is a POST with the CSRF token; a GET never changes state.
- **Nothing in GitHub Actions by default.** Scheduled work is a routine of the hub, not a hosted workflow.
- **Secrets never as values**, not in code, tests, logs, issues or pull requests: only the variable name.
- **Neutral examples.** Use `ana@exemplo.test` and `*.test` domains; no real names, domains or paths.
- A mistake that happened twice becomes a test, a hook or a filter.

## Conduct and security

Be kind: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Found a vulnerability? Do not open a public issue:
[SECURITY.md](SECURITY.md).
