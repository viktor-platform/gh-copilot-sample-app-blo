import argparse
import json
import logging
import re
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from etabs_model import EtabsError, EtabsUnavailable, run_shed
from shed import Shed
from viewer import model_payload, render_view


ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
RUN_LOCK = Lock()
LOG = logging.getLogger(__name__)


class ShedHandler(BaseHTTPRequestHandler):
    def loopback_origin(self) -> bool:
        port = self.server.server_port
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        if port == (443 if scheme == "https" else 80):
            allowed.update({"127.0.0.1", "localhost"})
        origin = self.headers.get("Origin")
        if host in allowed and (origin is None or origin == f"{scheme}://{host}"):
            return True
        self.send_json(HTTPStatus.FORBIDDEN, {"error": "Use the local app URL."})
        return False

    def send_content(self, status: HTTPStatus, body: bytes, media_type: str, *, download: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="steel-shed.edb"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: HTTPStatus, data: dict[str, object]) -> None:
        self.send_content(status, json.dumps(data, allow_nan=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not self.loopback_origin():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.send_content(HTTPStatus.OK, render_view(Shed()).encode("utf-8"), "text/html; charset=utf-8")
            return
        model = re.fullmatch(r"/api/models/([0-9a-f]{32})", path)
        if model:
            path_to_model = RUNS / model[1] / "steel-shed.edb"
            if (path_to_model.parent / "complete").is_file() and path_to_model.is_file():
                self.send_content(HTTPStatus.OK, path_to_model.read_bytes(), "application/octet-stream", download=True)
                return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def do_POST(self) -> None:
        if not self.loopback_origin():
            return
        path = urlsplit(self.path).path
        if path not in {"/api/preview", "/api/run"}:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send the input as JSON."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16_384:
                raise ValueError("Send a JSON input of up to 16 KB.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Send one JSON object with the shed inputs.")
            shed = Shed.from_values(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Send valid JSON."})
            return
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/preview":
            self.send_json(HTTPStatus.OK, model_payload(shed))
            return
        self.run_analysis(shed)

    def run_analysis(self, shed: Shed) -> None:
        if not RUN_LOCK.acquire(blocking=False):
            self.send_json(HTTPStatus.CONFLICT, {"error": "ETABS is running another analysis. Wait for it to finish."})
            return
        try:
            run_id, result, method = run_shed(shed, RUNS)
        except EtabsUnavailable as error:
            LOG.info("%s", error, exc_info=True)
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
        except EtabsError as error:
            LOG.exception("ETABS did not complete the analysis.")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        except Exception:
            LOG.exception("Unexpected ETABS error.")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ETABS stopped before returning results. Check the terminal log."})
        else:
            self.send_json(HTTPStatus.OK, {
                **model_payload(shed, result), "model_url": f"/api/models/{run_id}", "build_method": method,
            })
        finally:
            RUN_LOCK.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local ETABS steel shed app.")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--cert", type=Path, help="TLS certificate for localhost")
    parser.add_argument("--key", type=Path, help="Private key for the TLS certificate")
    args = parser.parse_args()
    if (args.cert is None) != (args.key is None):
        parser.error("Provide --cert and --key together.")
    if not 0 <= args.port <= 65_535:
        parser.error("Use a port between 0 and 65535.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with ThreadingHTTPServer(("127.0.0.1", args.port), ShedHandler) as server:
        scheme = "http"
        if args.cert:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(args.cert, args.key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        print(f"Open {scheme}://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
