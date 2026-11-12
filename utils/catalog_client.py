"""Babylon catalog API and SSO credentials. Never log response bodies or secrets.

Protocol reference: redhat-cop/babylon catalog/api/app.py and cli/pkg/api/
at commit 353883b656dac6cdcde73b34d7705225bbfb8655.
The unusual Authentication header is intentional: it is NOT Authorization.
"""

import http.cookiejar
import json
import os
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import requests
import yaml


DEFAULT_SERVER = "https://catalog.demo.redhat.com"


class CatalogError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def server_url(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("Catalog server must be an HTTPS origin without credentials or a path")
    return value.rstrip("/")


def secure_write(path, data):
    """Atomically replace a private JSON file; temporary files also have mode 0600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("Refusing to write through a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".aia-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON file (contents suppressed)") from None


def cookie_map(path, server):
    """Import JSON cookie maps, browser storage-state JSON, or Netscape cookies."""
    text = Path(path).read_text()
    host = urlsplit(server).hostname
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        jar = http.cookiejar.MozillaCookieJar(str(path))
        try:
            # Filter expiration below so Netscape session cookies with expiry 0
            # are retained, consistently with the JSON import formats.
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            raise ValueError("Expected cookie JSON or a Netscape cookie file") from None
        data = [{"name": c.name, "value": c.value, "domain": c.domain,
                 "expires": c.expires} for c in jar]
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, dict):
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
            raise ValueError("Cookie map must contain string names and values")
        return data
    if not isinstance(data, list):
        raise ValueError("Expected a cookie map or browser cookie list")
    cookies = {}
    for cookie in data:
        domain = str(cookie.get("domain", "")).lstrip(".")
        expiry = cookie.get("expires")
        if domain and (host == domain or host.endswith("." + domain)):
            if expiry and expiry > 0 and expiry < time.time():
                continue
            cookies[cookie["name"]] = cookie["value"]
    if not cookies:
        raise ValueError("No unexpired cookies for the catalog host in that file")
    return cookies


def load_credentials(state_dir, server, babylon_config=None):
    own = Path(state_dir) / "auth.json"
    if babylon_config is None and own.exists():
        config = read_json(own)
    else:
        path = Path(babylon_config) if babylon_config else Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "babylon/config.yaml"
        if not path.exists():
            raise CatalogError("No catalog login. Run: python utils/dev-environment.py login")
        try:
            config = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            raise ValueError("Invalid Babylon config YAML (contents suppressed)") from None
        if not isinstance(config, dict):
            raise ValueError("Invalid Babylon config")
    if config.get("server", "").rstrip("/") != server:
        raise CatalogError("Credential server differs from --server; log in to the selected server")
    auth = config.get("auth") or {}
    cookies = auth.get("cookies") or {}
    if not isinstance(cookies, dict) or not cookies:
        raise CatalogError("No SSO proxy cookies saved; run login again or import a cookie file")
    return cookies


def browser_login(server, timeout=300, open_browser=True):
    """Use Babylon's browser SSO handoff; no SSO password ever enters this process."""
    nonce = secrets.token_urlsafe(32)
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/callback/" + nonce or received:
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError()
                fields = parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
                cookies = {k[7:]: v[0] for k, v in fields.items() if k.startswith("cookie_")}
                if not fields.get("token") or not cookies:
                    raise ValueError()
            except (ValueError, UnicodeError):
                self.send_error(400)
                return
            received.update(cookies)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Login received. Return to the terminal to verify the session.\n")

    class LoopbackServer(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(5)
            return connection, address

    with LoopbackServer(("127.0.0.1", 0), Callback) as listener:
        listener.timeout = 1
        callback = f"http://127.0.0.1:{listener.server_port}/callback/{nonce}"
        login_url = server + "/auth/cli-redirect?" + urlencode({"callback": callback})
        print("Complete SSO in your browser (existing browser sessions can be reused):")
        print(login_url)
        print("If this server lacks /auth/cli-redirect, use login --cookies-file instead.", flush=True)
        if open_browser:
            webbrowser.open(login_url)
        end = time.monotonic() + timeout
        while not received and time.monotonic() < end:
            listener.handle_request()
        if not received:
            raise CatalogError("SSO callback timed out. Retry login or import an exported cookie file")
    return received


class CatalogClient:
    def __init__(self, server, cookies, auth_path=None, http=None):
        self.server = server_url(server)
        self.http = http or requests.Session()
        self.http.cookies.update(cookies)
        self.auth_path = auth_path
        self.session = None
        self.expires = 0

    def _request(self, method, path, body=None):
        headers = {"Accept": "application/json"}
        if self.session and path != "/auth/session":
            headers["Authentication"] = "Bearer " + self.session["token"]
        try:
            response = self.http.request(method, self.server + path, json=body,
                                         headers=headers, timeout=30, allow_redirects=False)
        except requests.RequestException:
            message = ("Catalog read request failed; retry the command"
                       if method == "GET" else
                       "Catalog request failed; submission may have reached the server. Resume this run")
            raise CatalogError(message) from None
        if 300 <= response.status_code < 400 or "text/html" in response.headers.get("Content-Type", ""):
            raise CatalogError("SSO session expired or login required. Run login, then resume this run")
        if response.status_code >= 400:
            # Error bodies can echo submitted credentials. Never include them.
            raise CatalogError(f"Catalog API returned HTTP {response.status_code}; inspect the service in the catalog",
                               response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise CatalogError("Catalog returned invalid JSON (response suppressed)") from None
        if not isinstance(data, dict):
            raise CatalogError("Catalog returned an unexpected response shape")
        if self.auth_path:
            secure_write(self.auth_path, {"server": self.server, "auth": {
                "cookies": self.http.cookies.get_dict()}})
        return data

    def authenticate(self):
        session = self._read_with_retry("/auth/session")
        if not session.get("token") or not session.get("user") or not session.get("userNamespace"):
            raise CatalogError("Catalog session is missing token, user, or userNamespace")
        self.session = session
        self.expires = time.monotonic() + max(1, session.get("lifetime", 300) - 30)
        return session

    def request(self, method, path, body=None):
        if not self.session or time.monotonic() >= self.expires:
            self.authenticate()
        try:
            return self._read_with_retry(path) if method == "GET" else self._request(method, path, body)
        except CatalogError as error:
            if error.status != 401:
                raise
            self.authenticate()
            return self._read_with_retry(path) if method == "GET" else self._request(method, path, body)

    def _read_with_retry(self, path):
        for attempt in range(3):
            try:
                return self._request("GET", path)
            except CatalogError as error:
                transient = error.status in (429, 502, 503, 504) or str(error).startswith("Catalog read request failed")
                if not transient or attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def catalog(self, reference):
        namespace, name = reference.split("/", 1)
        return self.request("GET", f"/apis/babylon.gpte.redhat.com/v1/namespaces/{quote(namespace, safe='')}/catalogitems/{quote(name, safe='')}")

    @staticmethod
    def claim_path(namespace, name=None):
        path = f"/apis/poolboy.gpte.redhat.com/v1/namespaces/{quote(namespace, safe='')}/resourceclaims"
        return path + "/" + quote(name, safe="") if name else path

    def claim(self, namespace, name):
        return self.request("GET", self.claim_path(namespace, name))
