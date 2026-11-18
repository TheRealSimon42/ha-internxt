"""Data models for aiointernxt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Credentials:
    """What a client needs to persist: the rotating JWT and the plain mnemonic."""

    token: str = field(repr=False)
    mnemonic: str = field(repr=False)


@dataclass(slots=True, frozen=True)
class SecurityDetails:
    """Result of the first login step."""

    two_factor: bool
    encrypted_salt: str


@dataclass(slots=True, frozen=True)
class User:
    """Account data needed for drive and network calls."""

    uuid: str
    email: str
    user_id: str = field(repr=False)  # derives the static network password
    bridge_user: str
    bucket: str
    root_folder_uuid: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> User:
        return cls(
            uuid=data["uuid"],
            email=data["email"],
            user_id=data["userId"],
            bridge_user=data["bridgeUser"],
            bucket=data["bucket"],
            root_folder_uuid=data["rootFolderId"],
        )


@dataclass(slots=True, frozen=True)
class DriveFolder:
    """A folder in Internxt Drive."""

    uuid: str
    name: str
    parent_uuid: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DriveFolder:
        return cls(
            uuid=data["uuid"],
            name=data.get("plainName") or data.get("name") or "",
            parent_uuid=data.get("parentUuid"),
        )


@dataclass(slots=True, frozen=True)
class DriveFile:
    """A file in Internxt Drive.

    `uuid` addresses the drive entry (delete, meta), `file_id` the encrypted
    object in the network bucket (download). Empty files have no `file_id`.
    """

    uuid: str
    file_id: str | None
    plain_name: str
    type: str
    size: int
    folder_uuid: str
    modification_time: str | None

    @property
    def name(self) -> str:
        """Display name with extension, as every Internxt client shows it."""
        return f"{self.plain_name}.{self.type}" if self.type else self.plain_name

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DriveFile:
        return cls(
            uuid=data["uuid"],
            file_id=data.get("fileId") or None,
            plain_name=data.get("plainName") or data.get("name") or "",
            type=data.get("type") or "",
            size=int(data.get("size") or 0),
            folder_uuid=data.get("folderUuid") or "",
            modification_time=data.get("modificationTime"),
        )


def split_name(file_name: str) -> tuple[str, str]:
    """Split like Go's filepath.Ext: last dot, a leading-dot-only name has no type."""
    base = file_name.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return base, ""
    return base[:dot], base[dot + 1 :]
