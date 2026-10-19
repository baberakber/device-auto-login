import base64
import ssl
import threading
from urllib.parse import urlsplit

import requests
import urllib3
import websocket
from flask import Flask, Response, redirect, render_template, request
from flask_sock import Sock
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

import config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEVICE_ORIGIN = f"https://{config.DEVICE_IP}"
PROXY_ORIGIN = f"http://{config.LAUNCHER_HOST}:{config.PROXY_PORT}"
DEVICE_IP_BYTES = config.DEVICE_IP.encode()
TEXT_MARKERS = ("text/", "json", "javascript", "xml", "html", "svg")
SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "cookie",
    "authorization",
    "accept-encoding",
    "origin",
    "referer",
}
SKIP_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
}


class DeviceGateway:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.lock = threading.Lock()
        self.ready = False

    def session_alive(self):
        if not self.ready or not self.session.cookies:
            return False
        try:
            check = self.session.get(
                f"{DEVICE_ORIGIN}/status.xml",
                timeout=10,
                allow_redirects=False,
            )
        except RequestException:
            return False
        return check.status_code == 200

    def login(self):
        with self.lock:
            if self.session_alive():
                return True, "Signed in."
            return self._begin_session()

    def _begin_session(self):
        credentials = f"{config.USERNAME}:{config.PASSWORD}".encode()
        authorization = "Basic " + base64.b64encode(credentials).decode("ascii")
        try:
            if self.session.cookies:
                try:
                    self.session.post(
                        f"{DEVICE_ORIGIN}/xmlapi/session/end",
                        timeout=10,
                        allow_redirects=False,
                    )
                except RequestException:
                    pass
            self.session.cookies.clear()
            begin = self.session.post(
                f"{DEVICE_ORIGIN}/xmlapi/session/begin",
                headers={"Authorization": authorization},
                timeout=20,
                allow_redirects=False,
            )
            if begin.status_code < 200 or begin.status_code >= 300:
                self.ready = False
                detail = "Too many open sessions on the device. " if b"Too many open sessions" in begin.content else ""
                return False, f"{detail}Session creation failed with status {begin.status_code}."

            self.ready = True
            return True, "Signed in."
        except RequestException as exc:
            self.ready = False
            return False, f"Could not reach {config.DEVICE_IP}. {exc.__class__.__name__}."

    def cookie_header(self):
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.session.cookies)

    def fetch(self, method, path, query, headers, body):
        url = f"{DEVICE_ORIGIN}/{path}"
        if query:
            url = f"{url}?{query}"
        upstream = self.session.request(
            method,
            url,
            headers=headers,
            data=body,
            timeout=60,
            allow_redirects=False,
        )
        return upstream.status_code, list(upstream.headers.items()), upstream.content


gateway = DeviceGateway()
launcher = Flask(__name__)
proxy = Flask(__name__, static_folder=None)
sock = Sock(proxy)


@launcher.route("/")
def index():
    return render_template("index.html", ip=config.DEVICE_IP)


@launcher.route("/connect", methods=["GET", "POST"])
def connect():
    ok, message = gateway.login()
    if not ok:
        return render_template("error.html", message=message), 502
    return redirect(f"{PROXY_ORIGIN}/")


def rewrite_text(payload):
    replacements = {
        DEVICE_ORIGIN: PROXY_ORIGIN,
        f"http://{config.DEVICE_IP}": PROXY_ORIGIN,
        f"wss://{config.DEVICE_IP}": PROXY_ORIGIN.replace("http://", "ws://", 1),
        f"ws://{config.DEVICE_IP}": PROXY_ORIGIN.replace("http://", "ws://", 1),
    }
    for source, target in replacements.items():
        payload = payload.replace(source, target)
    return payload


def rewrite_location(value):
    parts = urlsplit(value)
    if parts.netloc == config.DEVICE_IP:
        return PROXY_ORIGIN + parts.path + (f"?{parts.query}" if parts.query else "")
    return rewrite_text(value)


@proxy.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@proxy.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def forward(path):
    if not gateway.ready:
        return "Open the device from the launcher page first.", 401

    headers = {}
    for key, value in request.headers.items():
        if key.lower() in SKIP_REQUEST_HEADERS:
            continue
        headers[key] = value
    headers["Host"] = config.DEVICE_IP
    headers["Origin"] = DEVICE_ORIGIN
    headers["Referer"] = DEVICE_ORIGIN + "/"

    try:
        status, upstream_headers, body = gateway.fetch(
            request.method,
            path,
            request.query_string.decode("latin1"),
            headers,
            request.get_data(),
        )
    except RequestException:
        return "The device closed the connection.", 502

    if status == 401:
        ok, _message = gateway.login()
        if ok:
            try:
                status, upstream_headers, body = gateway.fetch(
                    request.method,
                    path,
                    request.query_string.decode("latin1"),
                    headers,
                    request.get_data(),
                )
            except RequestException:
                return "The device closed the connection.", 502

    content_type = ""
    response_headers = []
    for key, value in upstream_headers:
        lower = key.lower()
        if lower in SKIP_RESPONSE_HEADERS:
            continue
        if lower == "location":
            value = rewrite_location(value)
        if lower == "content-type":
            content_type = value.lower()
        response_headers.append((key, value))

    if any(marker in content_type for marker in TEXT_MARKERS) and DEVICE_IP_BYTES in body:
        body = rewrite_text(body.decode("utf-8", errors="replace")).encode("utf-8")

    return Response(body, status=status, headers=response_headers)


@sock.route("/ws")
def device_socket(browser_ws):
    query = request.query_string.decode("latin1")
    upstream_url = f"wss://{config.DEVICE_IP}/ws"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    device_ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
    try:
        device_ws.connect(
            upstream_url,
            cookie=gateway.cookie_header(),
            origin=DEVICE_ORIGIN,
            host=config.DEVICE_IP,
            suppress_origin=False,
        )
    except Exception:
        browser_ws.close()
        return

    closed = threading.Event()

    def device_to_browser():
        try:
            while not closed.is_set():
                message = device_ws.recv()
                if message is None or message == "":
                    break
                browser_ws.send(message)
        except Exception:
            pass
        finally:
            closed.set()

    threading.Thread(target=device_to_browser, daemon=True).start()
    try:
        while not closed.is_set():
            message = browser_ws.receive(timeout=1)
            if message is None:
                continue
            device_ws.send(message)
    except Exception:
        pass
    finally:
        closed.set()
        device_ws.close()


def serve_proxy():
    proxy.run(
        host=config.LAUNCHER_HOST,
        port=config.PROXY_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    threading.Thread(target=serve_proxy, daemon=True).start()
    print(f"Launcher: http://{config.LAUNCHER_HOST}:{config.LAUNCHER_PORT}")
    launcher.run(
        host=config.LAUNCHER_HOST,
        port=config.LAUNCHER_PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
