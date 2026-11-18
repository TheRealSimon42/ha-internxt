"""Proof of concept against a real Internxt account.

Run:  ../.venv/bin/python poc.py
Asks for e-mail, password and (if active) the 2FA code in the terminal. Nothing secret
is written to disk; the result file only contains measurements and error types.

Every wrong password or 2FA code counts toward Internxt's lock after 10 failures,
so the script never retries a login on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import getpass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from aiointernxt import (
    Credentials,
    InternxtAccountError,
    InternxtAuthError,
    InternxtClient,
    InternxtError,
    InternxtTwoFactorRequired,
)
from aiointernxt import client as client_mod

CLIENT_NAME = "ha-internxt-poc"
FOLDER = "ha-internxt-poc"
CHECK_FILE = "ha-internxt-formatprobe.txt"
CHECK_TEXT = (
    "Hallo aus Home Assistant! Wenn du diesen Text in der Internxt-Web-App lesen kannst, "
    "legt die Integration Dateien im normalen Internxt-Format ab.\n"
)
MIB = 1024 * 1024
SIZES = [5 * MIB, 150 * MIB]  # single PUT and multipart (threshold 100 MiB, parts 16 MiB)
RESULT = Path(__file__).with_name(f"ergebnis-{time.strftime('%Y-%m-%d_%H%M')}.json")

results: dict[str, Any] = {"client": CLIENT_NAME, "start": time.strftime("%Y-%m-%d %H:%M:%S")}


def save() -> None:
    RESULT.write_text(json.dumps(results, indent=2, default=str))


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)
    save()


def record_error(where: str, err: BaseException) -> None:
    results.setdefault("fehler", []).append(
        {"schritt": where, "typ": type(err).__name__, "text": str(err)[:300]}
    )
    print(f"  FEHLER in {where}: {type(err).__name__}: {err}")
    save()


async def random_stream(size: int, digest: Any) -> AsyncIterator[bytes]:
    left = size
    while left:
        chunk = os.urandom(min(MIB, left))
        digest.update(chunk)
        left -= len(chunk)
        yield chunk


async def login(client: InternxtClient) -> bool:
    email = os.environ.get("INTERNXT_EMAIL") or input("Internxt-E-Mail: ").strip()
    password = getpass.getpass("Passwort (wird nicht angezeigt): ")
    details = await client.security_details(email)
    tfa = input("2FA ist aktiv. Aktueller 6-stelliger Code: ").strip() if details.two_factor else None
    try:
        await client.login(email, password, tfa)
    except InternxtTwoFactorRequired as err:
        record_error("login", err)
        print("2FA-Code abgelehnt. Bitte auf einen frischen Code warten und das Skript neu starten.")
        return False
    except InternxtAccountError as err:
        record_error("login", err)
        print("Das Konto erlaubt keinen Drittzugriff oder ist gesperrt. Bitte nicht erneut versuchen.")
        return False
    except InternxtAuthError as err:
        record_error("login", err)
        print("Login abgelehnt (Passwort?). Jeder Fehlversuch zählt, bitte erst prüfen, dann neu starten.")
        return False
    finally:
        del password
    expires = client.token_expires_at
    hours = round((expires.timestamp() - time.time()) / 3600, 1) if expires else None
    results["login"] = {"two_factor": details.two_factor, "token_valid_h": hours}
    print(f"OK. Token läuft in {hours} h ab.")
    return True


async def check_refresh(session: aiohttp.ClientSession, client: InternxtClient) -> None:
    old = client.credentials.token
    try:
        await client.refresh()
    except InternxtError as err:
        record_error("refresh", err)
        print("  Refresh fehlgeschlagen, mache mit dem Login-Token weiter.")
        return
    new = client.credentials.token
    probe = InternxtClient(session, client_name=CLIENT_NAME)
    probe.set_credentials(Credentials(token=old, mnemonic=client.credentials.mnemonic))
    root = client.user.root_folder_uuid

    async def token_valid(c: InternxtClient) -> bool | None:
        try:
            await c.list_folders(root)
        except InternxtAuthError:
            return False
        except InternxtError as err:
            record_error("token-test", err)
            return None
        return True

    old_valid = await token_valid(probe)
    new_valid = await token_valid(client)
    results["refresh"] = {
        "rotates": new != old,
        "old_token_still_valid": old_valid,
        "new_token_valid": new_valid,
    }
    print(f"  Neues Token: {new != old}, altes noch gültig: {old_valid}, neues gültig: {new_valid}")


def spy_presigned(client: InternxtClient) -> None:
    """Record lifetime-related query parameters of presigned URLs (no signature values)."""
    original = client._start_upload

    async def spy(size: int, parts: int) -> dict[str, Any]:
        upload = await original(size, parts)
        url = (upload.get("urls") or [upload.get("url")])[0] or ""
        query = parse_qs(urlparse(url).query)
        keep = ("X-Amz-Expires", "X-Amz-Date", "Expires")
        results.setdefault("presigned", []).append({
            "host": urlparse(url).hostname,
            "parts": parts,
            "query_keys": sorted(query),
            **{k: query[k][0] for k in keep if k in query},
        })  # fmt: skip
        save()
        return upload

    client._start_upload = spy  # type: ignore[method-assign]


async def transfer(client: InternxtClient, folder_uuid: str, size: int) -> dict[str, Any]:
    name = f"poc_{size // MIB}MiB_{int(time.time())}.tar"
    entry: dict[str, Any] = {"size": size, "multipart": size >= client_mod.MULTIPART_THRESHOLD}
    src, last = hashlib.sha256(), [0.0]

    def progress(sent: int) -> None:
        if time.monotonic() - last[0] > 2 or sent == size:
            last[0] = time.monotonic()
            print(f"  hoch: {sent / MIB:7.1f} / {size / MIB:.0f} MiB", flush=True)

    uploaded = None
    try:
        t0 = time.monotonic()
        uploaded = await client.upload(
            random_stream(size, src), size=size, folder_uuid=folder_uuid, name=name,
            progress=progress,
        )  # fmt: skip
        entry["upload_s"] = round(time.monotonic() - t0, 1)
        t0 = time.monotonic()
        dst, n = hashlib.sha256(), 0
        async with await client.download(uploaded) as stream:
            async for chunk in stream:
                dst.update(chunk)
                n += len(chunk)
        entry["download_s"] = round(time.monotonic() - t0, 1)
        entry["identical"] = src.digest() == dst.digest() and n == size
        print(f"  {name}: hoch {entry['upload_s']}s ({size / MIB / max(entry['upload_s'], 0.1):.1f} MiB/s), "
              f"runter {entry['download_s']}s, identisch: {entry['identical']}")  # fmt: skip
    except InternxtError as err:
        record_error(f"transfer {size // MIB} MiB", err)
        entry["error"] = type(err).__name__
    finally:
        if uploaded is not None:
            try:
                await client.delete_file(uploaded.uuid)
                entry["deleted"] = True
            except InternxtError as err:
                record_error(f"löschen {name}", err)
    return entry


def _gb(value: Any) -> str:
    return f"{int(value) / 1e9:.1f} GB" if isinstance(value, (int, float)) and value else "?"


def describe_account(account: dict[str, Any]) -> str:
    """Readable one-liner; the raw answers only go into the result file."""
    usage, limit = account.get("usage"), account.get("limit")
    used = usage.get("total") if isinstance(usage, dict) else None
    if used is None and isinstance(usage, dict):
        used = sum(v for v in usage.values() if isinstance(v, (int, float)))
    total = limit.get("maxSpaceBytes") if isinstance(limit, dict) else None
    return (f"Speicher belegt: {_gb(used)} von {_gb(total)}, "
            f"größte erlaubte Datei: {_gb(account.get('max_upload_size'))}")  # fmt: skip


def summary() -> None:
    ok, bad = "\u2705", "\u274c"
    lines = [f"{ok if 'login' in results else bad} Login"]
    refresh = results.get("refresh") or {}
    lines.append(f"{ok if refresh.get('new_token_valid') else bad} Token erneuern")
    for t in results.get("transfers") or []:
        art = "in Teilen" if t.get("multipart") else "am Stück"
        mark = ok if t.get("identical") else bad
        lines.append(f"{mark} {t['size'] // MIB} MiB {art}: hoch {t.get('upload_s', '?')} s, "
                     f"runter {t.get('download_s', '?')} s, byte-genau gleich: {t.get('identical', False)}")  # fmt: skip
    lines.append(f"{ok if results.get('formatprobe_hochgeladen') else bad} Formatprobe liegt in Internxt")
    print("\n=== Ergebnis\n  " + "\n  ".join(lines))
    if results.get("fehler"):
        print(f"  {len(results['fehler'])} Fehler, Details in {RESULT.name}")


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = InternxtClient(session, client_name=CLIENT_NAME)

        step("1. Login")
        if not await login(client):
            return

        step("2. Konto")
        for key, call in (("usage", client.usage), ("limit", client.limit),
                          ("max_upload_size", client.max_upload_size)):  # fmt: skip
            try:
                results.setdefault("account", {})[key] = await call()
            except InternxtError as err:
                record_error(key, err)
        print(f"  {describe_account(results.get('account') or {})}")

        step("3. Token-Refresh")
        await check_refresh(session, client)

        step("4. Testordner")
        try:
            folder = await client.ensure_folder(client.user.root_folder_uuid, FOLDER)
        except InternxtError as err:
            record_error("ordner", err)
            return
        print(f"  Ordner '{FOLDER}' bereit.")

        step("5. Upload, Download, Vergleich")
        spy_presigned(client)
        results["transfers"] = [await transfer(client, folder.uuid, size) for size in SIZES]

        step("6. Formatprobe für die Internxt-Web-App")
        try:
            existing = {f.name: f for f in await client.list_files(folder.uuid)}
            if CHECK_FILE in existing:
                await client.delete_file(existing[CHECK_FILE].uuid)
            await client.upload_bytes(CHECK_TEXT.encode(), folder_uuid=folder.uuid, name=CHECK_FILE)
            print(f"  Bitte in drive.internxt.com den Ordner '{FOLDER}' öffnen und '{CHECK_FILE}'")
            print("  ansehen oder herunterladen. Ist der Text lesbar, klappt auch ein Notfall-Restore ohne HA.")
            results["formatprobe_hochgeladen"] = True
        except InternxtError as err:
            record_error("formatprobe", err)

        step("7. Gegenprobe: Datei aus der Web-App lesen (optional)")
        print(f"  Lade per drive.internxt.com eine kleine Textdatei in den Ordner '{FOLDER}'.")
        name = input("  Ihr Name (Enter = überspringen): ").strip()
        if name:
            try:
                files = {f.name: f for f in await client.list_files(folder.uuid)}
                if name in files:
                    text = (await client.download_bytes(files[name]))[:300]
                    print(f"  Inhalt (Anfang): {text!r}")
                    results["web_file_check"] = {"readable": True}
                else:
                    print(f"  Nicht gefunden. Vorhanden: {sorted(files)}")
                    results["web_file_check"] = {"readable": False, "reason": "not found"}
            except InternxtError as err:
                record_error("web-datei", err)

    results["ende"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save()
    summary()
    print(f"\nErgebnis gespeichert: {RESULT}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
    except Exception as err:  # noqa: BLE001 - keep the measurements, show the error
        record_error("unerwartet", err)
        raise
    finally:
        save()
