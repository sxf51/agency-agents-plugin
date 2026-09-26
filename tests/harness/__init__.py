"""Development and test support that works with or without the project present.

`select_host()` decides which half of the plugin API is available: the project's
own PluginManager when it imports, the stand-ins here otherwise. Everything else
is built on that one choice.

| Module | What it does |
| --- | --- |
| `host` | `RealHost`, `FakeHost` and `select_host()` behind one interface |
| `shim` | Stand-ins for `extension.plugin` / `.hook` / `.plugin_web`, so plugin source needs no change |
| `loader` | Manifest, config schema, command and requirements rules for the standalone side |
| `fakes` | Stand-in tool registry, hook manager, subagent registry, storage, web context, executor |
| `checks` | Checks derived from the plugin's own declarations; shared by the tests and `main.py doctor` |
| `pageaudit` | Static checks on `pages/`, including the endpoints the page's JS calls |
| `report` | The capability inventory printed after tests and by `main.py inspect` |
| `devserver` | `main.py serve`: the page in a real browser against real endpoints |
| `rename` | `main.py rename`: rename a copied plugin in one pass |

None of it names this plugin's tools, commands or endpoints, so it copies to a
new plugin unchanged. Its only dependency is PyYAML.
"""

from .host import Bundle, FakeHost, RealHost, select_host

__all__ = ["Bundle", "FakeHost", "RealHost", "select_host"]
