# aiointernxt

Async Python client for [Internxt Drive](https://internxt.com): login (incl. 2FA), token refresh,
client-side AES-256-CTR encryption, streaming upload (single and multipart) and download with
integrity check, folders, delete.

Protocol ported from [internxt/rclone-adapter](https://github.com/internxt/rclone-adapter) (MIT).
Third-party access uses Internxt's CLI login endpoint, available on Ultimate and Business plans.
