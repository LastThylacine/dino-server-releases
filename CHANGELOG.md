# Changelog

## Dino Server 1.0.18 - 2026-07-28

- Restored native `Random player` Bucks mail for existing server-owned
  pre-schema wallets. The server migrates a legacy wallet automatically only
  when its balance is internally consistent and supported by both a positive
  `MAILGIFT` transaction and prior delivery evidence. Arbitrary or guessed
  legacy metadata remains client-owned.
- Fixed tournament opponent loading by advertising only saves with replayable
  profile and battle state, preserving composite callback order, and keeping
  healthy post-login sessions alive during slow client loading.
- Fixed battle cooldown corruption by preserving the opaque `batl.104` packed
  values, replaying them as SmartFox long arrays, and removing speculative
  server-side timer arithmetic.
- Fixed the Brontosaurus speed-up network error by keeping repeated client
  transactions distinct and preserving the following battle-state field type.
- Protected existing player levels and linked-save account identities from
  stale level-1/profile writes, and made custom save-ID migration copy-only
  with no overwrite of either save.
- Fixed Bucks transaction deduplication across reconnects by using the native
  operation ID, added provenance-gated wallet state and conservative bounded
  trusted-backup recovery with a dedicated wallet marker, and stopped legacy
  saves from receiving a guessed balance. Legacy wallets with no trusted state
  can be initialized offline from an observed balance with
  `manage_players.py init-hardcash`; the command locks and revision-checks the
  save, creates a byte-exact backup, and changes only the wallet sidecar.
  Rolling retention keeps its trusted marker separate and always preserves
  the newest ordinary save snapshot.
- Preserved the original native Bucks mailbox protocol: the client renders the
  award as a `Random player` gift, `c.mg` and `c.mc` use the established static
  gift identity and compact payload, and collection immediately marks the
  amount delivered for the live session. No custom sender, title, transaction
  acknowledgement shape, or direct player-specific balance override is used.
- Fixed battle and tournament disconnect/crash paths by failing closed on
  locked or invalid saves and keeping healthy post-login sessions open through
  idle read windows while still detecting closed clients.
- Fixed reused PID and stale same-install listener handling without terminating
  unproven processes.
- Added matching iOS and Android regression coverage. Physical device and
  emulator acceptance remains required before release.
- Linux support is unchanged.

## Dino Server 1.0.17 - 2026-07-26

- Fixed the restarted PyInstaller launcher inheriting a stale `_MEI` runtime
  directory and showing a `Failed to load Python DLL` error after a successful
  update.
- No server, cache, offer, save or gameplay behavior was changed.

## Dino Server 1.0.16 - 2026-07-26

- Test release for verifying the automatic update and restart path introduced
  in 1.0.15.
- No server, cache, offer, save or gameplay behavior was changed.

## Dino Server 1.0.15 - 2026-07-26

- Redesigned the whole interface around a dark jungle palette: deeper obsidian
  surfaces, a vivid green reserved for the ready state, amber for pending work
  and red only for real failures.
- Replaced the flat overview header with a gradient status hero that states
  whether the server is stopped, starting or accepting devices without reading
  the console.
- Rebuilt the sidebar with grouped navigation and a selection marker that
  glides to the active page, and moved the version into a footer chip.
- Added a platform badge to the title bar and to the guide so the selected iOS
  or Android profile is always visible. The two profiles keep separate caches,
  manifests and data.
- Replaced the metric strip with individual tiles and gave the overview a
  services card, an activity card and clearer empty states.
- Added a "What's new" window that opens once after an update, reads the
  bundled `CHANGELOG.md` so it works offline, and can be reopened from About.
- Added a text-only preview of the published GitHub release notes before an
  update is installed. No markup, HTML or remote content is rendered and no
  link is ever opened automatically.
- Added Appearance settings: reduce animations and show release notes after an
  update. Settings pages now scroll, so no hint is clipped in any of the four
  languages.
- Fixed the General and Project files tabs so their content switches reliably.
- Fixed the Computer / DNS Address controls and the stopped-server banner at
  Windows display scaling above 100%, preventing clipped or overlapping text.
- Fixed automatic updates being terminated with the PyInstaller launcher:
  the update helper now starts through the Windows shell broker and survives
  long enough to replace and restart `DinoServer.exe`.
- Application-only update. Caches, saves, save backups, linked devices, allow
  lists, generated manifests and local settings are preserved, and no game
  cache has to be downloaded again.

## Dino Server 1.0.14 - 2026-07-26

- Physical Android testing confirmed that the 1.0.13 surface add-on visibility
  fix displays Indominus.
- Switched Indominus from free to the client's native hardcash purchase path
  and set its reviewed `HardCashPrice` to `499`.
- Replaced the permanently pinned three-day offer with a real calendar
  schedule: offers are active Saturday 00:00 UTC through Monday 00:00 UTC and
  are hidden during the week.
- Added a deterministic six-week cycle containing a surface dinosaur and one
  known building each weekend. Aquatic and glacier add-on lists advance on the
  same weekly index.
- Tightened the DNA Rescue event window to the same two UTC weekend days.
- Expanded iOS/Android tests for the price, deadline, weekday shutdown, next
  weekend rotation and transformed manifest checksums.
- Added a compatibility bridge for the 1.0.8 one-file launcher so its built-in
  updater waits out the PyInstaller parent-process lock and replaces
  `DinoServer.exe` last.
- The current update helper now waits for the launcher executable to become
  replaceable, retries transient copy failures and records failures in
  `logs/update_error.log` without letting a failed rollback hide the cause.

## Dino Server 1.0.13 - 2026-07-26

- Recovered the actual client-side surface-add-on visibility gate from the
  original game binary: only `AmberRarity = 2` evaluates `ADDON_DINO_ID` and
  `ADDON_DINO_LIMIT`; Indominus was still marked premium with value `6`.
- The reviewed runtime transformation now changes Indominus `AmberRarity`
  from `6` to `2` and `CashType` from `3` to `0`. Source Android and iOS cache
  files remain untouched and unknown game-data revisions remain unsupported.
- Changed the transformed-asset cache key and expanded iOS/Android unit and
  runtime-smoke coverage for the add-on classification and manifest checksum.
- This is an application-only update; no new APK, IPA, or cache download is
  required. Physical offer visibility and collection remain acceptance checks.

## Dino Server 1.0.12 - 2026-07-26

- Fixed the split online-offer state that kept Indominus hidden: `c.pb`
  advertised Indominus while the `c.rp` product catalogue still returned the
  donor `Build_HotAirBalloon_5x5` item.
- The product catalogue now resolves the same current server-controlled
  online-options snapshot as the live options callback.
- Limited-offer enable, market IDs, and market deadlines are included in every
  live options refresh so an existing installation can update without
  downloading the full cache again.
- Added regression coverage for both iOS and Android catalogue resolution.

## Dino Server 1.0.11 - 2026-07-25

- Added a second native visibility path for Indominus through
  `MARKET_ITEM_ID_1` while retaining the normal surface add-on.
- The transformed response keeps the original client resource name
  `gamedata.dsb` and publishes its matching checksum. A physical Android test
  proved that renaming this resource produces a `0/0 MB` download followed by
  a startup crash, so the incompatible alias approach was removed.
- Removed the synthetic `onlineoptions` manifest entry after physical logs
  proved that the 4.9.0 client ignores it; offer values remain in the native
  login `oo` / `c.pb` refresh flow.
- Embedded DNS now accepts clients in the IPv4 shared-address range
  `100.64.0.0/10`, fixing setups where the direct status URL worked but DNS
  queries from the phone were silently dropped.
- Language-switch geometry is fully calculated behind the loading window
  before the completed interface is revealed.
- Added integration coverage for the original manifest filename, transformed
  checksum, source-cache preservation, and the absence of unsupported
  synthetic entries.

## Dino Server 1.0.10 - 2026-07-25

- Attempted to fix the invisible Indominus add-on after `c.pb` reported the
  correct ID and future deadline.
- The runtime manifest included a patched `onlineoptions` payload, but physical
  logs later proved that the 4.9.0 client does not request that synthetic
  manifest item. Version 1.0.11 removes this ineffective path.
- Both iOS and Android use the same reviewed offer options while retaining
  their separate cache and server branches.
- Added local HTTP integration coverage for the injected manifest entry,
  exact payload checksum and size, and preservation of the source cache files.

## Dino Server 1.0.9 - 2026-07-25

- Pinned the surface add-on offer to Indominus on both iOS and Android.
- Recovered the original game-data field that marks the Indominus offer as an
  external-store purchase and changes only that reviewed `CashType` value from
  `3` (store) to `0` (free) while the cache is served.
- The prepared `cache_ios` and `cache_android` files remain untouched. The
  response transformation accepts only the two known SHA-256 inputs and skips
  unknown game-data revisions instead of applying an offset-only patch.
- Manifest responses now publish the MD5 of the transformed `gamedata.dsb`, so
  an existing client cache can detect and download the one-byte update.
- Added unit and local HTTP integration coverage for both platform profiles,
  exact one-byte mutation, idempotence, manifest checksum matching, and source
  cache preservation. No APK or IPA patch is required.

## Dino Server 1.0.8 - 2026-07-25

- Added non-blocking update checks against the public
  `LastThylacine/dino-server-releases` GitHub Releases feed.
- Added a visible update notice, localized update status, and one-button
  download, SHA-256 verification, installation, and restart flow.
- Automatic updates use a strict software-only allow-list. They never package
  or replace either cache, guest saves, save backups, generated manifests,
  linked devices, allow lists, machine settings, logs, diagnostics, or run
  state.
- Added transactional file replacement with rollback if any update file cannot
  be applied.
- Added a dedicated `DinoServer-Update-vX.Y.Z.zip` release asset and checksum.

## Dino Server 1.0.7 - 2026-07-25

- Fixed the launcher losing its live server/DNS workers when Windows hides
  process command lines from non-elevated WMI/CIM queries.
- Managed frozen workers are now recovered from their exact executable path,
  tracked PID file, and process creation time. The UI no longer falsely shows
  `Stopped` or reports its own DNS worker as `PORT_53_OCCUPIED`.
- Added an iOS connection-path diagnostic that distinguishes no DNS query,
  no device HTTP request, and a game that has not opened TCP 9933 yet.
- Added the required iOS `Local Network` permission to the built-in guide in
  every language. A real iPhone DNS/browser/game connection was confirmed
  after enabling this permission.

## Dino Server 1.0.6 - 2026-07-25

- Fixed a false `FIREWALL_RULE_MISSING` startup failure after the user
  successfully approved the Windows UAC prompt.
- Firewall inspection now accepts both textual and numeric NetSecurity values,
  avoiding locale/build-specific parsing failures.
- A successful restricted `LocalSubnet` helper run is no longer rejected only
  because the non-elevated follow-up inspection is unavailable.

## Dino Server 1.0.5 - 2026-07-25

- Rebased both server branches and save concurrency on the known-good scripts
  from `DinosaurGamePrivateServer-Portable\jpb_server`.
- Removed the later Android wallet normalization and direct `c.gc` changes
  associated with hardcash save regressions.
- Preserved the portable hardcash sidecar guard, one-writer protection,
  Aquatic/Glacier handling and card-pack logic.
- Reapplied only the local `/setup/` guide endpoint required by the one-click
  launcher; save and wallet behavior remains on the portable baseline.
- Restored Android premium-currency delivery through the in-game mailbox
  instead of forcing the diagnostic direct `c.gi` mode.
- Fixed stopped-server diagnostics treating an automatically rebuildable
  manifest as a fatal error.
- Split cache counts into required present, additional, and total files
  (iOS 479 + 1; Android 566 + 4).

## Dino Server 1.0.4 - 2026-07-25

- Made shutdown idempotent when Windows briefly locks `dino-dns.pid`.
- Added bounded PID-file deletion retries and suppressed the harmless race
  between the DNS child cleanup and launcher shutdown.

## Dino Server 1.0.3 - 2026-07-25

- Detects and removes the legacy `Dino Server one-click local server` Public
  TCP/UDP block rules that override the newer LocalSubnet allow rules and
  prevent real devices from reaching the server.
- Fixed the elevated Firewall helper failing when the Dino Server installation
  path contains spaces.

## Dino Server 1.0.2 - 2026-07-25

- Fixed inactive CustomTkinter pages remaining visible above the selected
  page, which caused Overview and Diagnostics to overlap after state changes.
- Inactive pages are now removed from the geometry manager instead of relying
  only on Tk stacking order.
- Fixed Start/Restart creating additional permanent state-polling loops, which
  could flood the Tk event queue and freeze the launcher after a restart.
- Added a UI regression assertion that exactly one page is managed at a time.

## Dino Server 1.0.1 - 2026-07-24

- Removed the Public-network blocker and manual Windows Settings instruction.
- Firewall setup now covers Private and Public profiles while remaining
  restricted to `LocalSubnet` and the required TCP/UDP ports.
- Added a read-only two-profile cache checker with built-in catalogs for 479
  iOS and 566 Android files, including exact missing and wrong-size filenames.
- Added a scrollable cache report and a scrollable long-error dialog.
- Rebuilt Diagnostics with a two-row action layout and a scrollable result
  list so controls and errors stay inside the window.
- Replaced ambiguous phone-page wording with explicit setup-QR labels and
  added a visible explanation of what the QR opens.
- Hid partially constructed panels behind a complete loading window during
  first launch and language changes.
- Shortened the brand subtitle from one-click wording to `Local Server`.

## Dino Server 1.0.0 - 2026-07-24

- Renamed the public product and executable to Dino Server / `DinoServer.exe`.
- Added one-click coordinated startup with rollback and managed shutdown.
- Added exact UDP/TCP DNS overrides for the two game domains with safe upstream
  forwarding and no unrelated query-name logging.
- Added restricted Windows Firewall setup for Private + LocalSubnet only.
- Added occupied-port ownership diagnostics and a named single-instance mutex.
- Added stable iOS/Android cache, manifest and server-branch switching.
- Restored the verified clean iOS branch with no dynamic LPKG rewrite.
- Kept the newer Android direct `c.gi`/`c.gc` hardcash persistence logic.
- Added embedded iOS and Hosts Manager Lite Android guides and local `/setup/`.
- Added an offline-generated QR for opening the local `/setup/` page by phone.
- Added privacy-sanitized copy/save diagnostic reports.
- Added release checksums, clean staging and privacy scanning.
- Switched the launcher to a modern dark-green high-contrast theme.
