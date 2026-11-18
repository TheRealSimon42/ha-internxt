"""Entpackt ein verschlüsseltes Home-Assistant-Backup mit dem Backup-Schlüssel.

Aufruf:  .venv-ha/bin/python scripts/backup_entpacken.py <Backup-Ordner oder .tar>
Der Schlüssel wird verdeckt abgefragt (oder aus der Umgebungsvariable HA_BACKUP_KEY gelesen).
Ergebnis: <Backup-Ordner>/entpackt/<homeassistant|ssl|...>/
Nutzt securetar, die Bibliothek, mit der Home Assistant selbst ver- und entschlüsselt.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
import sys
import tarfile

from securetar import InvalidPasswordError, SecureTarFile, SecureTarReadError


def _safe_filter(skipped: list[str]):
    """Like tarfile's "data" filter, but skips unsafe members (e.g. absolute symlinks) instead of aborting."""

    def flt(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError as err:
            skipped.append(f"{member.name} ({type(err).__name__})")
            return None

    return flt


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).expanduser()
    if source.is_file():  # ganze .tar aus HA: erst äußere Hülle auspacken (unverschlüsselt)
        folder = source.with_suffix("")
        folder.mkdir(exist_ok=True)
        with tarfile.open(source) as outer:
            outer.extractall(folder, filter="data")
    else:
        folder = source
    inner = sorted(folder.glob("*.tar.gz")) + sorted(folder.glob("*.tar"))
    if not inner:
        print(f"Keine .tar.gz in {folder} gefunden.")
        return 1

    key = os.environ.get("HA_BACKUP_KEY") or getpass.getpass("Backup-Schlüssel (wird nicht angezeigt): ")
    key = key.strip()
    target = folder / "entpackt"
    skipped: list[str] = []
    for archive in inner:
        name = archive.name.removesuffix(".gz").removesuffix(".tar")
        out = target / name
        out.mkdir(parents=True, exist_ok=True)
        try:
            with SecureTarFile(archive, password=key, gzip=archive.suffix == ".gz") as tar:
                tar.extractall(out, filter=_safe_filter(skipped))
        except (InvalidPasswordError, SecureTarReadError, tarfile.ReadError) as err:
            print(f"{archive.name}: Schlüssel falsch oder Datei beschädigt ({type(err).__name__}).")
            return 1
        files = sum(1 for p in out.rglob("*") if p.is_file())
        print(f"{archive.name}: {files} Dateien nach {out}")
    if skipped:
        print(f"{len(skipped)} Verknüpfungen auf Pfade außerhalb des Backups übersprungen (zeigen ins HA-System, für dich unwichtig):")
        for item in skipped[:20]:
            print(f"  - {item}")
        if len(skipped) > 20:
            print(f"  … und {len(skipped) - 20} weitere")
    print(f"Fertig. Alles liegt unter {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
