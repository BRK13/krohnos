# Changelog

## 0.1.0 (2026-10-17)

First public release of Krohnos (formerly Krohnos Hub).

### Included

- **Harness**: the agent harness that runs scheduled tasks, issue work and routines for teams of AI agents (stdlib only).
- **Dashboard (painel)**: web panel with the plugin menu and routes.
- **Goals (metas)**: goals with progress tracking in the panel.
- **Vault (cofre)**: encrypted secret storage (optional `cryptography` extra); only names are ever shown.
- **Connections (conexões)**: connect external services from the panel; paid ones (such as DataForSEO) are optional and off by default.
- **Login with roles**: account login with permission roles for the panel.
- **Demo**: `krohnos painel demo` loads sample data so you can look around before connecting anything.
- **Installation**: five-step quickstart in the README (clone `https://github.com/BRK13/krohnos.git`, then run with Docker Compose); `pip install` via `pyproject.toml`.
- **Plugins**: extension points for routes, menu entries and routines (see `docs/PLUGINS.md`).
- **SEO audit and data collector**: site checks (Search Console, PageSpeed, seomator) and the data collector.

### Not included

Kept out of the open code: prospecting and WhatsApp funnels, reels and Instagram tooling, procurement and local-market
modules of the original owner, dashboards over private application databases, and the Jarvis/Gmail integration.
