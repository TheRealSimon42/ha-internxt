# Contributing

Pull requests are welcome. Want to help long-term? Open a [maintainer issue](../../issues/new?template=maintainer.yml).

## Layout

- `aiointernxt/`: async client for Internxt Drive (no Home Assistant code). Tests run against a local fake of the Internxt API.
- `custom_components/internxt/`: the integration. It ships a copy of `aiointernxt` until the library is on PyPI; after changing the library run `scripts/sync_lib.sh`.
- `tests/`: integration tests with `pytest-homeassistant` (Home Assistant 2026.11+) or `pytest-homeassistant-custom-component` (older); `tests/common.py` picks whichever is installed.
- `poc/poc.py`: end-to-end check against a real account (asks for credentials, stores none).

## Ground rules

- **Never retry logins automatically.** Internxt locks an account after 10 failed attempts, wrong 2FA codes included.
- **Only the CLI login endpoint.** Third-party access is limited to Internxt Ultimate and Pro Business; working around that limit would break Internxt's terms.
- **No secrets in logs, errors or diagnostics** (password, token, mnemonic, presigned URLs).
- **Keep memory flat.** Backups can be many GB and many users run Home Assistant on a Raspberry Pi; stream, don't buffer.
- New behaviour comes with a test. Both test suites must stay green:

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp cryptography pytest pytest-asyncio pytest-timeout
(cd aiointernxt && ../.venv/bin/pip install -e . && ../.venv/bin/python -m pytest)
python3 -m venv .venv-ha && .venv-ha/bin/pip install "pytest-homeassistant==2026.11.2"
.venv-ha/bin/python -m pytest
```

When Internxt changes something, [internxt/rclone-adapter](https://github.com/internxt/rclone-adapter) usually follows first; it is the reference this client was ported from.
