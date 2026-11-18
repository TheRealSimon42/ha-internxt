# Internxt for Home Assistant

Store your Home Assistant backups end-to-end encrypted in [Internxt Drive](https://internxt.com/simon42).

**Deutsch:** Mit dieser Integration landen deine Home-Assistant-Backups direkt und Ende-zu-Ende-verschlüsselt in Internxt, ohne Umweg über das Rclone-Add-on. Internxt wird dazu einfach als zusätzlicher Speicherort in den Backup-Einstellungen von Home Assistant ausgewählt.

> **Requires Internxt Ultimate or Pro Business.** Other plans do not allow access by third-party apps; setup tells you if your plan doesn't fit.
> **Voraussetzung: Internxt Ultimate oder Pro Business.** Get Internxt: https://internxt.com/simon42

## 🙋 Maintainers wanted / Maintainer gesucht

Building an integration is the easy part, keeping it working when Internxt or Home Assistant change is the real work. If you'd like to help maintain this integration, [open a maintainer request](../../issues/new?template=maintainer.yml) and we'll add you as a collaborator.

Eine Integration zu bauen ist der leichte Teil, sie am Laufen zu halten die eigentliche Arbeit. Wenn du mitpflegen möchtest, [stell eine Maintainer-Anfrage](../../issues/new?template=maintainer.yml), dann tragen wir dich als Mitwirkenden ein.

## Features

- Internxt as a backup location in **Settings → System → Backups** (Home Assistant 2026.4 or newer)
- Streaming upload and download with flat memory use, also for multi-GB backups on a Raspberry Pi
- Client-side AES-256-CTR encryption in Internxt's own format: backups can also be downloaded in the Internxt web or desktop app
- Login with two-factor authentication; the password is used once and never stored
- Integrity check on every download, free-space check before every upload
- Old backups are deleted permanently (not to the trash), so retention frees space
- Diagnostics with e-mail, token and account key redacted; English and German
- **Actions** `internxt.upload` and `internxt.delete`: upload files from Home Assistant (for example camera snapshots from an automation) and delete them again, to the Internxt trash by default
- **Storage sensors**: used, free and total storage plus usage in percent
- **Media source**: browse your Internxt folders in the media browser and play images, videos and audio, with seeking

### Example: send a snapshot to Internxt when the doorbell rings

```yaml
actions:
  - action: camera.snapshot
    target:
      entity_id: camera.front_door
    data:
      filename: /media/snapshots/front_door.jpg
  - action: internxt.upload
    data:
      config_entry_id: YOUR_INTERNXT_ENTRY_ID  # pick the account in the action editor
      filename: /media/snapshots/front_door.jpg
      destination_folder: Home Assistant Uploads/Kameras
      overwrite: true
```

Files must be in a folder Home Assistant may read, like `/media` or `/config/www`, or one listed under `allowlist_external_dirs`.

## Installation

[![Open this repository in your Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=TheRealSimon42&repository=ha-internxt&category=integration)

**Marketplace (Home Assistant 2026.11 and newer):** click the button above, or add `https://github.com/TheRealSimon42/ha-internxt` as a repository from a link in **Settings → Marketplace**, install **Internxt**, restart Home Assistant.

<!-- ha-marketplace:exclude-start -->
**HACS (Home Assistant before 2026.11):** HACS → ⋮ → *Custom repositories* → add `https://github.com/TheRealSimon42/ha-internxt` as type *Integration*, install **Internxt**, restart Home Assistant.
<!-- ha-marketplace:exclude-end -->

**Manual:** copy `custom_components/internxt` into `config/custom_components/` and restart Home Assistant.

## Setup

[![Add the Internxt integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=internxt)

1. **Settings → Devices & services → Add integration → Internxt**, log in with e-mail and password (plus 2FA code if enabled). Setup creates the folder *Home Assistant Backups* and runs a short connection test.
2. **Settings → System → Backups → Backup settings**: select Internxt as a location.

## Good to know

- **Two layers of encryption.** Internxt encrypts on your device before upload; Home Assistant additionally encrypts the backup with your backup key. Keep both your Internxt password and your Home Assistant backup key safe.
- **Restore without this integration.** Download the `.tar` in the Internxt web or desktop app and upload it during Home Assistant onboarding. `scripts/backup_entpacken.py` decrypts a backup on your computer with the backup key.
- **Your account key is stored in Home Assistant** (like every cloud login). Keep Home Assistant backups encrypted and your instance secured.
- **Failed logins count.** Internxt locks an account after 10 failed logins; the integration never retries a rejected password on its own.
- File names are visible to Internxt, file contents are not.

## Repository layout

| Path | Content |
|---|---|
| `custom_components/internxt/` | The Home Assistant integration (config flow with 2FA and reauth, backup agent, diagnostics, de/en) |
| `aiointernxt/` | Async Python client for Internxt Drive: login, token refresh, AES-256-CTR, streaming up- and download |
| `tests/` | Integration tests (`pytest-homeassistant`) |
| `poc/poc.py` | End-to-end check against a real account |
| `scripts/` | Library sync and backup decryption helper |

See [CONTRIBUTING.md](CONTRIBUTING.md) for development.

## License

MIT. The protocol implementation is ported from [internxt/rclone-adapter](https://github.com/internxt/rclone-adapter) (MIT). This is a community project, not an official Internxt product.
