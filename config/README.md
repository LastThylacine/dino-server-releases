# Configuration files

Dino Server tracks the runtime configuration that is required for the server to start and validate supported client data.

Tracked runtime files include:

- `cache_index_android.json`
- `cache_index_ios.json`
- `fixed_manifest.json`
- `fixed_manifest_android.json`
- `fixed_manifest_ios.json`
- `onlineoptions`
- `offer_rotation.json`

These files are part of the Dino Server runtime package and are intentionally versioned together with the source code.

The repository does **not** contain game cache payloads. The `cache_android/` and `cache_ios/` directories contain placeholder guidance only; users provide any required local client-side files themselves.

The following files contain local user/runtime state and are intentionally ignored by Git:

- `device_links.json`
- `whitelist.json`

Do not commit personal save data, credentials, device identifiers, or game cache files.
