"""HTTP client for the LocalizeMe API, on the standard library only.

Errors from the API carry a human-readable ``message``; surfacing that instead of
a status code is most of what makes a CLI usable when something is wrong.
"""

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import __version__

# With the version, so the API can tell which releases are still in use before
# anything they depend on changes.
USER_AGENT = f'localizeme-cli/{__version__}'


class ApiError(Exception):
    """The API refused a request. ``message`` is what the API said."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NetworkError(Exception):
    """The API could not be reached at all."""


def _encode_multipart(fields: dict, filename: str, content: bytes) -> tuple[bytes, str]:
    """Build a multipart/form-data body for the one-file upload the API takes."""
    boundary = f'----localizeme{uuid.uuid4().hex}'
    content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    parts: list[bytes] = []

    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
    )
    parts.append(content)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    return b''.join(parts), f'multipart/form-data; boundary={boundary}'


class Client:
    def __init__(self, api_url: str, api_key: str, timeout: int = 60):
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params=None, body=None, content_type=None):
        url = f'{self.api_url}{path}'
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f'{url}?{urllib.parse.urlencode(query)}'

        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
        }
        if content_type:
            headers['Content-Type'] = content_type

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            raise ApiError(self._message_from(exc), status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise NetworkError(f'Could not reach {self.api_url}: {exc.reason}') from exc
        except TimeoutError as exc:
            raise NetworkError(f'{self.api_url} did not respond within {self.timeout}s') from exc

    @staticmethod
    def _message_from(exc: urllib.error.HTTPError) -> str:
        """The API's own message where there is one, so the user reads prose."""
        try:
            payload = json.loads(exc.read().decode('utf-8'))
            message = payload.get('message') or payload.get('detail')
            if message:
                return str(message)
        except (ValueError, OSError, AttributeError):
            pass
        if exc.code == 401:
            return 'API key was rejected. Check LOCALIZEME_API_KEY.'
        if exc.code == 403:
            return 'That key does not have access to this project.'
        return f'{exc.code} {exc.reason}'

    def get_json(self, path: str, params=None) -> dict:
        _, raw, _ = self._request('GET', path, params=params)
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError as exc:
            raise ApiError(f'{path} returned something that is not JSON') from exc

    def get_bytes(self, path: str, params=None) -> tuple[bytes, dict]:
        """Raw body plus headers, for the export endpoints that return files."""
        _, raw, headers = self._request('GET', path, params=params)
        return raw, headers

    def post_file(self, path: str, fields: dict, filename: str, content: bytes) -> dict:
        body, content_type = _encode_multipart(fields, filename, content)
        _, raw, _ = self._request('POST', path, body=body, content_type=content_type)
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError as exc:
            raise ApiError(f'{path} returned something that is not JSON') from exc
