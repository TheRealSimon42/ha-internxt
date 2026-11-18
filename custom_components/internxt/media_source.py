"""Browse and play files from Internxt Drive in the Home Assistant media browser."""

from __future__ import annotations

import logging
import mimetypes
from typing import override

from aiohttp import web

from homeassistant.components import http
from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from . import InternxtConfigEntry
from .aiointernxt import DriveFile, InternxtError, InternxtNotFoundError
from .const import DOMAIN, MEDIA_VIEW_URL

_LOGGER = logging.getLogger(__name__)

MEDIA_CLASSES = {
    "image": MediaClass.IMAGE,
    "video": MediaClass.VIDEO,
    "audio": MediaClass.MUSIC,
}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Set up the Internxt media source."""
    hass.http.register_view(InternxtMediaView(hass))
    return InternxtMediaSource(hass)


def _mime(name: str) -> str | None:
    mime, _ = mimetypes.guess_type(name)
    return mime if mime and mime.split("/")[0] in MEDIA_CLASSES else None


def _loaded_entry(hass: HomeAssistant, entry_id: str) -> InternxtConfigEntry | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        return None
    return entry


class InternxtMediaSource(MediaSource):
    """Internxt Drive folders with images, videos and audio.

    Identifiers: "" (accounts), "<entry_id>" (root), "<entry_id>/f/<folder_uuid>/<folder name>",
    "<entry_id>/i/<file_uuid>/<file name>".
    """

    name = "Internxt"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    @override
    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/", 3)
        if len(parts) != 4 or parts[1] != "i":
            raise Unresolvable(f"Not a file: {item.identifier}")
        entry_id, _, file_uuid, name = parts
        mime = _mime(name)
        if mime is None or _loaded_entry(self.hass, entry_id) is None:
            raise Unresolvable(f"Cannot play {name}")
        return PlayMedia(f"{MEDIA_VIEW_URL}/{entry_id}/{file_uuid}/{name}", mime)

    @override
    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        entries: list[InternxtConfigEntry] = (
            self.hass.config_entries.async_loaded_entries(DOMAIN)
        )
        if not entries:
            raise BrowseError("Internxt is not set up")
        if not item.identifier:
            if len(entries) == 1:
                return await self._browse_folder(entries[0], None, "Internxt")
            return BrowseMediaSource(
                domain=DOMAIN,
                identifier=None,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title="Internxt",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
                children=[self._folder_node(e, None, e.title) for e in entries],
            )
        parts = item.identifier.split("/", 3)
        entry = _loaded_entry(self.hass, parts[0])
        if entry is None:
            raise BrowseError("Internxt account not loaded")
        if len(parts) >= 3 and parts[1] == "f":
            return await self._browse_folder(
                entry, parts[2], parts[3] if len(parts) == 4 else ""
            )
        return await self._browse_folder(entry, None, entry.title)

    def _folder_node(
        self, entry: InternxtConfigEntry, folder_uuid: str | None, title: str
    ) -> BrowseMediaSource:
        identifier = (
            entry.entry_id
            if folder_uuid is None
            else f"{entry.entry_id}/f/{folder_uuid}/{title}"
        )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
        )

    def _file_node(
        self, entry: InternxtConfigEntry, file: DriveFile, mime: str
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry.entry_id}/i/{file.uuid}/{file.name}",
            media_class=MEDIA_CLASSES[mime.split("/")[0]],
            media_content_type=mime,
            title=file.name,
            can_play=True,
            can_expand=False,
        )

    async def _browse_folder(
        self, entry: InternxtConfigEntry, folder_uuid: str | None, title: str
    ) -> BrowseMediaSource:
        client = entry.runtime_data.client
        uuid = folder_uuid or client.user.root_folder_uuid
        try:
            folders = await client.list_folders(uuid)
            files = await client.list_files(uuid)
        except InternxtError as err:
            raise BrowseError(f"Internxt: {err}") from err
        children = [
            self._folder_node(entry, f.uuid, f.name)
            for f in sorted(folders, key=lambda f: f.name.lower())
        ]
        for file in sorted(files, key=lambda f: f.name.lower()):
            if mime := _mime(file.name):
                children.append(self._file_node(entry, file, mime))
        node = self._folder_node(entry, folder_uuid, title)
        node.children = children
        node.children_media_class = MediaClass.DIRECTORY
        return node


class InternxtMediaView(http.HomeAssistantView):
    """Stream a decrypted Internxt file, with HTTP range support for seeking."""

    url = MEDIA_VIEW_URL + "/{entry_id}/{file_uuid}/{name}"
    name = "api:internxt:media"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._meta: dict[str, DriveFile] = {}

    async def head(
        self, request: web.Request, entry_id: str, file_uuid: str, name: str
    ) -> web.StreamResponse:
        return await self.get(request, entry_id, file_uuid, name, head=True)

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        file_uuid: str,
        name: str,
        head: bool = False,
    ) -> web.StreamResponse:
        entry = _loaded_entry(self.hass, entry_id)
        mime = _mime(name)
        if entry is None or mime is None:
            raise web.HTTPNotFound
        client = entry.runtime_data.client
        try:
            file = self._meta.get(file_uuid) or await client.file_meta(file_uuid)
        except InternxtNotFoundError as err:
            raise web.HTTPNotFound from err
        except InternxtError as err:
            _LOGGER.debug("Internxt media meta failed: %s", err)
            raise web.HTTPBadGateway from err
        self._meta[file_uuid] = file
        if len(self._meta) > 500:
            self._meta.pop(next(iter(self._meta)))

        start, end, status = 0, file.size - 1, 200
        try:
            rng = request.http_range
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file.size}"}
            ) from None
        if rng.start is not None or rng.stop is not None:
            if rng.start is not None and rng.start < 0:  # suffix range "bytes=-N"
                start = max(file.size + rng.start, 0)
            else:
                start = rng.start or 0
                if rng.stop is not None:
                    end = min(rng.stop - 1, file.size - 1)
            if start > end or start >= file.size:
                raise web.HTTPRequestRangeNotSatisfiable(
                    headers={"Content-Range": f"bytes */{file.size}"}
                )
            status = 206

        response = web.StreamResponse(status=status)
        response.content_type = mime
        response.content_length = end - start + 1
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=3600"
        if status == 206:
            response.headers["Content-Range"] = f"bytes {start}-{end}/{file.size}"
        if head or file.size == 0:
            await response.prepare(request)
            return response

        try:
            stream = await client.download(file, start=start, end=end)
        except InternxtError as err:
            _LOGGER.debug("Internxt media download failed: %s", err)
            raise web.HTTPBadGateway from err
        await response.prepare(request)
        async with stream:
            try:
                async for chunk in stream:
                    await response.write(chunk)
            except InternxtError as err:
                _LOGGER.debug("Internxt media stream interrupted: %s", err)
                return response
            except ConnectionResetError:
                return response  # player closed the connection while seeking
        await response.write_eof()
        return response
