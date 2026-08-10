# Local configuration data

This source repository intentionally does not distribute game cache files or compatibility/configuration data that may originate from a local client installation.

Dino Server creates or consumes some files under `config/` at runtime. The following are intentionally ignored by Git:

- `device_links.json`
- `whitelist.json`
- `cache_index*.json`
- `fixed_manifest*.json`
- `onlineoptions`

These files are not required to understand or review the Dino Server source code. Users running a compatible client must provide any required local client-side data themselves and are responsible for ensuring they are entitled to use it.
