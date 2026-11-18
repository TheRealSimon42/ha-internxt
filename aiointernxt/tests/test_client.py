"""Client tests against a local fake of the Internxt drive, network and storage APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
import asyncio
import hashlib
import os
from typing import Any
import uuid

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from aiointernxt import (
    Credentials,
    InternxtAccountBlocked,
    InternxtAuthError,
    InternxtConnectionError,
    InternxtError,
    InternxtQuotaError,
    InternxtStreamError,
    InternxtClient,
    InternxtIntegrityError,
    InternxtPlanError,
    InternxtTwoFactorRequired,
    crypto,
)
from aiointernxt import client as client_mod

MNEMONIC = " ".join(["abandon"] * 11 + ["about"])
PASSWORD = "correct horse"
SALT_HEX = "a1b2c3d4e5f60718"
BUCKET = "0123456789abcdef01234567"
USER = {
    "uuid": "user-uuid",
    "email": "simon@example.com",
    "userId": "bridge-user-id",
    "bridgeUser": "simon@example.com",
    "bucket": BUCKET,
    "rootFolderId": "root-uuid",
}


class FakeInternxt:
    """Minimal stateful fake that enforces the protocol details we rely on."""

    def __init__(self, *, two_factor: bool = False, plan_ok: bool = True) -> None:
        self.two_factor = two_factor
        self.plan_ok = plan_ok
        self.strict_rotation = (
            False  # the real server keeps old tokens valid until they expire
        )
        self.refresh_calls = 0
        self.tokens = {"token-0"}
        self.put_failures: list[web.Response] = []  # served before successful PUTs
        self.create_file_failures: list[
            int
        ] = []  # statuses after creating the entry anyway
        self.info_override: Callable[[], web.Response] | None = (
            None  # fresh response per call
        )
        self.max_upload: int | None = None
        self.starts = 0
        self.discard_puts = (
            False  # memory test: don't keep uploaded data in this process
        )
        self.uploads: dict[str, dict[str, Any]] = {}  # upload uuid -> state
        self.objects: dict[
            str, dict[str, Any]
        ] = {}  # network file id -> {data, index, hash}
        self.files: dict[str, dict[str, Any]] = {}  # drive uuid -> entry
        self.folders: dict[str, dict[str, Any]] = {}
        self.put_headers: list[dict[str, str]] = []
        self.corrupt = False
        self.base = ""

    def app(self) -> web.Application:
        app = web.Application(client_max_size=1024**3)
        r = app.router
        r.add_post("/drive/auth/login", self.login)
        r.add_post("/drive/auth/cli/login/access", self.access)
        r.add_get("/drive/users/cli/refresh", self.refresh)
        r.add_get("/drive/folders/v2/content/{uuid}/{kind}", self.list_content)
        r.add_post("/drive/folders", self.create_folder)
        r.add_post("/drive/files", self.create_file)
        r.add_delete("/drive/files/{uuid}", self.delete_file)
        r.add_post("/network/v2/buckets/{bucket}/files/start", self.start)
        r.add_post("/network/v2/buckets/{bucket}/files/finish", self.finish)
        r.add_get("/network/buckets/{bucket}/files/{fid}/info", self.info)
        r.add_put("/storage/{uuid}/{part}", self.put)
        r.add_get("/storage/obj/{fid}", self.get_object)
        r.add_get("/drive/files/limits", self.limits)
        return app

    async def limits(self, request: web.Request) -> web.Response:
        self._bearer(request)
        return web.json_response({"maxUploadFileSize": self.max_upload})

    def _bearer(self, request: web.Request) -> None:
        assert request.headers["internxt-client"] == "test-client"
        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.tokens:
            raise web.HTTPUnauthorized(text='{"message":"Unauthorized"}')

    def _basic(self, request: web.Request) -> None:
        assert request.headers["Authorization"] == crypto.bridge_auth_header(
            USER["bridgeUser"], USER["userId"]
        )

    async def login(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"sKey": crypto.encrypt_text(SALT_HEX), "tfa": self.two_factor}
        )

    async def access(self, request: web.Request) -> web.Response:
        body = await request.json()
        expected = hashlib.pbkdf2_hmac(
            "sha1", PASSWORD.encode(), bytes.fromhex(SALT_HEX), 10000, 32
        ).hex()
        if crypto.decrypt_text(body["password"]) != expected:
            return web.json_response({"message": "Wrong login credentials"}, status=401)
        if self.two_factor and body.get("tfa") != "123456":
            return web.json_response(
                {"message": "Wrong 2-factor auth code"}, status=401
            )
        if not self.plan_ok:
            return web.json_response(
                {"message": "rclone access not allowed"}, status=402
            )
        user = {**USER, "mnemonic": crypto.encrypt_text(MNEMONIC, PASSWORD)}
        return web.json_response(
            {"newToken": "token-0", "token": "legacy", "user": user}
        )

    async def refresh(self, request: web.Request) -> web.Response:
        self._bearer(request)
        self.refresh_calls += 1
        await asyncio.sleep(0.01)
        new = f"token-{self.refresh_calls}"
        self.tokens = {new} if self.strict_rotation else self.tokens | {new}
        return web.json_response({"newToken": new, "user": USER})

    async def list_content(self, request: web.Request) -> web.Response:
        self._bearer(request)
        parent, kind = request.match_info["uuid"], request.match_info["kind"]
        source = self.files if kind == "files" else self.folders
        key = "folderUuid" if kind == "files" else "parentUuid"
        items = [v for v in source.values() if v[key] == parent]
        # two pages to exercise the cursor
        cursor = request.query.get("cursor")
        page, nxt = (
            (items[1:], None)
            if cursor
            else (items[:1], "c1" if len(items) > 1 else None)
        )
        return web.json_response({kind: page, "nextCursor": nxt})

    async def create_folder(self, request: web.Request) -> web.Response:
        self._bearer(request)
        body = await request.json()
        fid = str(uuid.uuid4())
        self.folders[fid] = {"uuid": fid, "plainName": body["plainName"],
                             "parentUuid": body["parentFolderUuid"]}  # fmt: skip
        return web.json_response(self.folders[fid], status=201)

    async def create_file(self, request: web.Request) -> web.Response:
        self._bearer(request)
        body = await request.json()
        assert body["encryptVersion"] == "03-aes"
        if body["size"]:
            assert body["fileId"] in self.objects
        if any(
            f["plainName"] == body["plainName"]
            and f["type"] == body["type"]
            and f["folderUuid"] == body["folderUuid"]
            for f in self.files.values()
        ):
            return web.json_response({"message": "File already exists"}, status=409)
        fid = str(uuid.uuid4())
        self.files[fid] = {**body, "uuid": fid, "size": str(body["size"])}
        if self.create_file_failures:
            return web.json_response(
                {"message": "upstream"}, status=self.create_file_failures.pop(0)
            )
        return web.json_response(self.files[fid], status=201)

    async def delete_file(self, request: web.Request) -> web.Response:
        self._bearer(request)
        if self.files.pop(request.match_info["uuid"], None) is None:
            return web.json_response({"message": "not found"}, status=404)
        return web.json_response({})

    async def start(self, request: web.Request) -> web.Response:
        self._basic(request)
        self.starts += 1
        n = int(request.query["multiparts"])
        body = await request.json()
        up = str(uuid.uuid4())
        self.uploads[up] = {"size": body["uploads"][0]["size"], "parts": {}, "n": n}
        urls = [
            f"{self.base}/storage/{up}/{i}?X-Amz-Signature=SECRETSIG" for i in range(n)
        ]
        entry: dict[str, Any] = {"index": 0, "uuid": up, "urls": urls}
        if n > 1:
            entry["UploadId"] = "mp-" + up
        else:
            entry["url"] = urls[0]
        return web.json_response({"uploads": [entry]})

    async def put(self, request: web.Request) -> web.Response:
        self.put_headers.append(dict(request.headers))
        if self.put_failures:
            await request.read()
            return self.put_failures.pop(0)
        assert "chunked" not in request.headers.get("Transfer-Encoding", "")
        data = await request.read()
        assert len(data) == int(request.headers["Content-Length"])
        up, part = request.match_info["uuid"], int(request.match_info["part"])
        self.uploads[up]["parts"][part] = b"" if self.discard_puts else data
        return web.Response(headers={"ETag": f'"etag-{part}"'})

    async def finish(self, request: web.Request) -> web.Response:
        self._basic(request)
        body = await request.json()
        shard = body["shards"][0]
        up = self.uploads[shard["uuid"]]
        data = b"".join(up["parts"][i] for i in sorted(up["parts"]))
        if self.discard_puts:
            fid = uuid.uuid4().hex[:24]
            self.objects[fid] = {
                "data": b"",
                "index": body["index"],
                "hash": shard["hash"],
            }
            return web.json_response(
                {"id": fid, "bucket": BUCKET, "index": body["index"]}
            )
        assert len(data) == up["size"]
        if up["n"] > 1:
            assert shard["UploadId"] == "mp-" + shard["uuid"]
            assert [p["PartNumber"] for p in shard["parts"]] == list(
                range(1, up["n"] + 1)
            )
            assert [p["ETag"] for p in shard["parts"]] == [
                f"etag-{i}" for i in range(up["n"])
            ]
        assert shard["hash"] == crypto.ripemd160(hashlib.sha256(data).digest()).hex()
        fid = uuid.uuid4().hex[:24]  # real network file ids are 24 hex chars
        self.objects[fid] = {
            "data": data,
            "index": body["index"],
            "hash": shard["hash"],
        }
        return web.json_response({"id": fid, "bucket": BUCKET, "index": body["index"]})

    async def info(self, request: web.Request) -> web.Response:
        self._basic(request)
        if self.info_override is not None:
            return self.info_override()
        fid = request.match_info["fid"]
        obj = self.objects[fid]
        return web.json_response({
            "index": obj["index"], "size": len(obj["data"]), "version": 2,
            "shards": [{"index": 0, "hash": obj["hash"], "url": f"{self.base}/storage/obj/{fid}"}],
        })  # fmt: skip

    async def get_object(self, request: web.Request) -> web.Response:
        data = self.objects[request.match_info["fid"]]["data"]
        if self.corrupt:
            data = bytes([data[0] ^ 1]) + data[1:]
        return web.Response(body=data)


@pytest.fixture
async def fake() -> AsyncIterator[tuple[FakeInternxt, InternxtClient]]:
    import aiohttp

    fake = FakeInternxt()
    server = TestServer(fake.app())
    await server.start_server()
    fake.base = str(server.make_url("")).rstrip("/")
    async with aiohttp.ClientSession() as session:
        yield (
            fake,
            InternxtClient(session, client_name="test-client", base_url=fake.base),
        )
    await server.close()


async def _chunks(data: bytes, size: int = 70_000) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def test_login_decrypts_mnemonic(fake) -> None:
    _, client = fake
    creds = await client.login("simon@example.com", PASSWORD)
    assert creds == Credentials(token="token-0", mnemonic=MNEMONIC)
    assert client.user.bucket == BUCKET


async def test_login_wrong_password(fake) -> None:
    _, client = fake
    with pytest.raises(InternxtAuthError) as err:
        await client.login("simon@example.com", "wrong")
    assert not isinstance(err.value, InternxtTwoFactorRequired)


async def test_login_two_factor(fake) -> None:
    server, client = fake
    server.two_factor = True
    with pytest.raises(InternxtTwoFactorRequired):
        await client.login("simon@example.com", PASSWORD)
    with pytest.raises(InternxtTwoFactorRequired):
        await client.login("simon@example.com", PASSWORD, "000000")
    await client.login("simon@example.com", PASSWORD, "123456")


async def test_login_plan_not_allowed(fake) -> None:
    server, client = fake
    server.plan_ok = False
    with pytest.raises(InternxtPlanError):
        await client.login("simon@example.com", PASSWORD)


async def test_refresh_rotates_token(fake) -> None:
    _, client = fake
    client.set_credentials(Credentials(token="token-0", mnemonic=MNEMONIC))
    await client.refresh()
    assert client.credentials.token == "token-1"
    assert client.user.root_folder_uuid == "root-uuid"
    client.set_credentials(Credentials(token="unknown", mnemonic=MNEMONIC))
    with pytest.raises(InternxtAuthError):
        await client.refresh()


@pytest.mark.parametrize("size", [0, 1, 1_000_000, 3_500_000])
async def test_upload_download_roundtrip(fake, monkeypatch, size: int) -> None:
    server, client = fake
    monkeypatch.setattr(client_mod, "MULTIPART_THRESHOLD", 2_000_000)
    monkeypatch.setattr(client_mod, "MIN_PART_SIZE", 1)
    await client.login("simon@example.com", PASSWORD)
    folder = await client.ensure_folder("root-uuid", "HA Backups")
    assert (await client.ensure_folder("root-uuid", "HA Backups")) == folder

    data = os.urandom(size)
    progress: list[int] = []
    f = await client.upload(
        _chunks(data), size=size, folder_uuid=folder.uuid, name="backup_2026.tar",
        progress=progress.append, part_size=1_000_000,
    )  # fmt: skip
    assert (f.plain_name, f.type, f.size, f.name) == (
        "backup_2026",
        "tar",
        size,
        "backup_2026.tar",
    )
    if size:
        stored = server.objects[f.file_id]["data"]
        assert stored != data  # encrypted at rest
        assert progress[-1] == size
        if size > 2_000_000:
            assert len(server.uploads) == 1
            assert next(iter(server.uploads.values()))["n"] == 4
    assert await client.download_bytes(f) == data

    listed = await client.list_files(folder.uuid)
    assert [x.uuid for x in listed] == [f.uuid]
    await client.delete_file(f.uuid)
    assert await client.list_files(folder.uuid) == []


async def test_upload_rejects_short_stream(fake) -> None:
    _, client = fake
    await client.login("simon@example.com", PASSWORD)
    with pytest.raises(InternxtError, match="ended after 100 of 200") as err:
        await client.upload(
            _chunks(b"x" * 100), size=200, folder_uuid="root-uuid", name="a.tar"
        )
    assert not isinstance(err.value, InternxtStreamError)


async def test_download_detects_corruption(fake) -> None:
    server, client = fake
    await client.login("simon@example.com", PASSWORD)
    f = await client.upload_bytes(
        b"metadata json", folder_uuid="root-uuid", name="m.metadata.json"
    )
    assert (f.plain_name, f.type) == ("m.metadata", "json")
    server.corrupt = True
    with pytest.raises(InternxtIntegrityError):
        await client.download_bytes(f)


async def test_listing_follows_cursor(fake) -> None:
    _, client = fake
    await client.login("simon@example.com", PASSWORD)
    for name in ("a.txt", "b.txt", "c.txt"):
        await client.upload_bytes(b"x", folder_uuid="root-uuid", name=name)
    assert sorted(f.name for f in await client.list_files("root-uuid")) == [
        "a.txt",
        "b.txt",
        "c.txt",
    ]


# ---------------------------------------------------------------- review findings


async def _failing(data: bytes, exc: Exception) -> AsyncIterator[bytes]:
    yield data
    raise exc


@pytest.mark.parametrize("size", [300_000, 3_000_000])  # single PUT and multipart
async def test_source_stream_error_is_stream_error(
    fake, monkeypatch, size: int
) -> None:
    server, client = fake
    monkeypatch.setattr(client_mod, "MULTIPART_THRESHOLD", 2_000_000)
    monkeypatch.setattr(client_mod, "MIN_PART_SIZE", 1)
    await client.login("simon@example.com", PASSWORD)
    with pytest.raises(InternxtStreamError) as err:
        await client.upload(
            _failing(b"x" * 1000, OSError("disk gone")), size=size, folder_uuid="root-uuid",
            name="a.tar", part_size=1_000_000,
        )  # fmt: skip
    assert isinstance(err.value.__cause__, OSError)
    assert "SECRETSIG" not in str(err.value)


async def test_multipart_rejects_long_stream(fake, monkeypatch) -> None:
    _, client = fake
    monkeypatch.setattr(client_mod, "MULTIPART_THRESHOLD", 2_000_000)
    monkeypatch.setattr(client_mod, "MIN_PART_SIZE", 1)
    await client.login("simon@example.com", PASSWORD)
    with pytest.raises(InternxtError, match="longer"):
        await client.upload(_chunks(b"x" * 2_500_001), size=2_500_000, folder_uuid="root-uuid",
                            name="a.tar", part_size=1_000_000)  # fmt: skip


def _connections(client: InternxtClient) -> int:
    return len(client._session.connector._acquired)  # type: ignore[union-attr]


async def test_download_releases_connection_when_never_read(fake) -> None:
    _, client = fake
    await client.login("simon@example.com", PASSWORD)
    f = await client.upload_bytes(
        os.urandom(500_000), folder_uuid="root-uuid", name="a.tar"
    )
    stream = await client.download(f)
    assert _connections(client) == 1
    await stream.aclose()
    assert _connections(client) == 0


async def test_download_releases_connection_on_early_stop(fake) -> None:
    _, client = fake
    await client.login("simon@example.com", PASSWORD)
    f = await client.upload_bytes(
        os.urandom(3_000_000), folder_uuid="root-uuid", name="a.tar"
    )
    async with await client.download(f) as stream:
        async for _ in stream:
            break
    assert _connections(client) == 0


async def test_no_secrets_in_reprs_and_storage_errors(fake) -> None:
    server, client = fake
    creds = await client.login("simon@example.com", PASSWORD)
    assert MNEMONIC not in repr(creds) and "token-0" not in repr(creds)
    assert USER["userId"] not in repr(client.user)
    server.put_failures = [
        web.Response(
            status=403,
            text="<Error><Code>SignatureDoesNotMatch</Code>"
            "<Signature>SECRETSIG</Signature></Error>",
        )  # fmt: skip
    ]
    with pytest.raises(InternxtError) as err:
        await client.upload_bytes(b"data", folder_uuid="root-uuid", name="a.tar")
    assert "SignatureDoesNotMatch" in str(err.value)
    assert "SECRETSIG" not in str(err.value)


async def test_unexpected_response_is_internxt_error(fake) -> None:
    server, client = fake
    await client.login("simon@example.com", PASSWORD)
    f = await client.upload_bytes(b"data", folder_uuid="root-uuid", name="a.tar")
    server.info_override = lambda: web.Response(
        text="<html>gateway</html>", content_type="text/html"
    )
    with pytest.raises(InternxtError, match="not JSON"):
        await client.download(f)
    server.info_override = lambda: web.json_response({"unexpected": True})
    with pytest.raises(InternxtError):
        await client.download(f)


async def test_part_retry_respects_retry_after(fake, monkeypatch) -> None:
    server, client = fake
    monkeypatch.setattr(client_mod, "MULTIPART_THRESHOLD", 2_000_000)
    monkeypatch.setattr(client_mod, "MIN_PART_SIZE", 1)
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    await client.login("simon@example.com", PASSWORD)
    server.put_failures = [
        web.Response(status=503),
        web.Response(status=429, headers={"Retry-After": "7"}),
    ]
    data = os.urandom(2_500_000)
    f = await client.upload(_chunks(data), size=len(data), folder_uuid="root-uuid", name="a.tar",
                            part_size=1_000_000)  # fmt: skip
    assert 7 in waits
    assert await client.download_bytes(f) == data


async def test_concurrent_refresh_shares_one_rotation(fake) -> None:
    server, client = fake
    server.strict_rotation = True
    client.set_credentials(Credentials(token="token-0", mnemonic=MNEMONIC))
    await asyncio.gather(client.refresh(), client.refresh())
    assert server.refresh_calls == 1
    assert client.credentials.token == "token-1"


async def test_quota_and_account_errors(fake) -> None:
    server, client = fake
    await client.login("simon@example.com", PASSWORD)
    server.max_upload = 1000
    with pytest.raises(InternxtQuotaError):
        await client.upload_bytes(b"x" * 1001, folder_uuid="root-uuid", name="big.tar")
    assert server.starts == 0  # rejected before uploading anything

    async def status(code: int, body: dict) -> None:
        server.info_override = lambda: web.json_response(body, status=code)
        await client.download(await client.upload_bytes(b"x", folder_uuid="root-uuid",
                                                        name=f"f{code}{len(body)}.tar"))  # fmt: skip

    server.max_upload = None
    with pytest.raises(InternxtQuotaError):
        await status(
            402, {"message": "File size exceeds", "error": "FILE_UPLOAD_SIZE_EXCEEDED"}
        )
    with pytest.raises(InternxtQuotaError):
        await status(420, {"error": "Max space used"})
    with pytest.raises(InternxtAccountBlocked):
        await status(403, {"message": "blocked", "error": "ACCOUNT_BLOCKED"})
    assert not issubclass(InternxtAccountBlocked, InternxtAuthError)  # no reauth loop
    with pytest.raises(InternxtConnectionError, match="Max space|upstream|503"):
        server.info_override = lambda: web.json_response(
            {"error": "upstream"}, status=503
        )
        await client.download(
            await client.upload_bytes(b"x", folder_uuid="root-uuid", name="z.tar")
        )


async def test_file_entry_recovered_after_lost_answer(fake) -> None:
    server, client = fake
    await client.login("simon@example.com", PASSWORD)
    server.create_file_failures = [504]  # entry is created, the answer is lost
    data = os.urandom(10_000)
    f = await client.upload_bytes(data, folder_uuid="root-uuid", name="a.tar")
    assert f.name == "a.tar"
    assert await client.download_bytes(f) == data


async def test_multipart_memory_stays_flat(fake, monkeypatch) -> None:
    import tracemalloc

    server, client = fake
    server.discard_puts = True
    monkeypatch.setattr(client_mod, "MULTIPART_THRESHOLD", 2_000_000)
    monkeypatch.setattr(client_mod, "MIN_PART_SIZE", 1)
    await client.login("simon@example.com", PASSWORD)
    part = 2_000_000
    block = os.urandom(64 * 1024)

    async def gen(total: int) -> AsyncIterator[bytes]:
        for _ in range(total // len(block)):
            yield block

    total = 160 * len(block) * 4  # ~40 MiB in 21 parts
    tracemalloc.start()
    await client.upload(
        gen(total), size=total, folder_uuid="root-uuid", name="m.tar", part_size=part
    )
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # two fixed part buffers plus the part the in-process fake server is reading
    assert peak < 5 * part, peak
