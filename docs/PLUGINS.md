# Writing a plugin

A plugin is a Python package named `kronos_plugin_<name>` in a folder you choose. It can add three things to the
hub, and nothing else is required:

| Hook | What it is | Used by |
|---|---|---|
| `ROTAS` | pages for the panel: `{"/path": function}` | the panel router |
| `MENU` | links in the panel menu: `[("Group", "/path", "Label")]` | the panel menu |
| `ROTINAS` | scheduled routines: `[("name", "python3 -m hub.x args")]` | `deploy/rotina.sh` |

The loader is `hub/plugins.py`.

## Turn it on

In `kronos.toml`:

```toml
[plugins]
caminho = "/opt/kronos/plugins"    # folder that holds the kronos_plugin_* packages
ligados = ["ola"]                  # names without the prefix; ["*"] turns on every package found
```

Only packages listed in `ligados` are read. A plugin that raises an exception on import does not take the panel down:
the error goes to stderr and to the warning on the `/monitor` page. A plugin that is off has no routes and no menu items
(its routes answer 404).

## A minimal example

Create `kronos_plugin_ola/__init__.py` in the plugins folder:

```python
import html

from hub import ui


def ola(q, **trechos):
    """Route function: q is the query string flattened to the first value of each key."""
    nome = html.escape(q.get("nome", "mundo"))
    return 200, ui.pagina("Olá", f"<p>Olá, {nome}!</p>", "/ola")


ROTAS = {"/ola": ola}
MENU = [("Exemplo", "/ola", "Olá")]
ROTINAS = [("ola-semanal", "python3 -m hub.ola semana")]
```

Open `/ola?nome=Ana` in the panel and the page answers `Olá, Ana!`.

## Rules for route functions

- Signature: `fn(q, **trechos) -> (status, html)` or `(status, html, headers)`. A path like `/bi/<negocio>/local`
  passes the `<negocio>` part as the keyword argument `negocio`.
- Build pages with `hub/ui.py` (`ui.pagina`), never with your own full HTML document.
- Escape every piece of text that comes from the user, the query string or a file (`html.escape`).
- A form that changes something must be a POST with the panel's CSRF token; a GET never changes state.
- Secrets never go in `kronos.toml` or in the plugin: read them from the vault.

## Routines

`ROTINAS` entries are `(name, command)`. The scheduler runs them through `deploy/rotina.sh <name> <command>`, with the same
lock and the same failure record as the built-in routines; see [INSTALAR.md](INSTALAR.md) ("Rodar uma rotina na mão").
