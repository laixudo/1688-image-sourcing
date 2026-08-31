# -*- coding: utf-8 -*-
"""独立诊断 stream-upload.taobao.com 的 DNS/TCP/TLS，不经过 Playwright。"""

from __future__ import annotations

import json
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

HOST = "stream-upload.taobao.com"
PORT = 443
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "storage" / "debug" / f"upload_connectivity_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "host": HOST,
        "port": PORT,
        "dns": {"ok": False},
        "tcp": {"ok": False},
        "tls": {"ok": False},
    }
    sock: socket.socket | None = None
    try:
        started = time.perf_counter()
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(HOST, PORT, type=socket.SOCK_STREAM)})
        result["dns"] = {
            "ok": True,
            "addresses": addresses,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

        started = time.perf_counter()
        sock = socket.create_connection((HOST, PORT), timeout=5)
        result["tcp"] = {
            "ok": True,
            "peer": list(sock.getpeername()),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }

        context = ssl.create_default_context()
        started = time.perf_counter()
        with context.wrap_socket(sock, server_hostname=HOST) as secure:
            sock = None
            cert = secure.getpeercert()
            result["tls"] = {
                "ok": True,
                "version": secure.version(),
                "cipher": secure.cipher()[0] if secure.cipher() else None,
                "subject": cert.get("subject"),
                "issuer": cert.get("issuer"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
    except socket.gaierror as exc:
        result["error_stage"] = "dns"
        result["error"] = f"{type(exc).__name__}: {exc}"
    except ssl.SSLError as exc:
        result["error_stage"] = "tls"
        result["error"] = f"{type(exc).__name__}: {exc}"
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        result["error_stage"] = "tcp_or_tls"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            sock.close()
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"log": str(out), **result}, ensure_ascii=False, indent=2, default=str))
    return 0 if result["tls"].get("ok") else 1  # type: ignore[union-attr]


if __name__ == "__main__":
    raise SystemExit(main())
