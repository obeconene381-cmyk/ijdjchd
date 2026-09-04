#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCP Proxy with PROXY protocol header injection for Xray.
Listens on 0.0.0.0:8080 (Cloud Run PORT), extracts client IP from
X-Forwarded-For, then forwards to Xray (127.0.0.1:5000) with PROXY protocol.
"""
import os
import socket
import socketserver
import threading
import sys

LISTEN_HOST = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PORT", "8080"))
BACKEND_HOST = os.environ.get("XRAY_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("XRAY_BACKEND_PORT", "5000"))
WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1")

def log(msg):
    print(f"[PROXY] {msg}", flush=True)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        try:
            data = self._read_headers(client)
            if not data:
                return
            headers_str = data.decode('latin-1', errors='ignore')
            method, path, _ = self._parse_request_line(headers_str)
            headers = self._parse_headers(headers_str)

            if path != WS_PATH or method.upper() != 'GET':
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                client.sendall(response)
                return

            client_ip = self._get_client_ip(headers, client)
            client_port = client.getpeername()[1]

            backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend.settimeout(30)
            backend.connect((BACKEND_HOST, BACKEND_PORT))

            # PROXY protocol header
            proxy_line = f"PROXY TCP4 {client_ip} {BACKEND_HOST} {client_port} {BACKEND_PORT}\r\n".encode()
            backend.sendall(proxy_line)
            backend.sendall(data)

            log(f"Proxying {client_ip} -> {BACKEND_HOST}:{BACKEND_PORT} path={path}")

            stop_event = threading.Event()
            t1 = threading.Thread(target=self._relay, args=(client, backend, stop_event))
            t2 = threading.Thread(target=self._relay, args=(backend, client, stop_event))
            t1.daemon = True
            t2.daemon = True
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        except Exception as e:
            log(f"Connection error: {e}")
        finally:
            client.close()

    def _read_headers(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break
        return data

    def _parse_request_line(self, headers_str):
        lines = headers_str.split("\r\n")
        if not lines:
            return "", "", ""
        parts = lines[0].split(" ")
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        return "", "", ""

    def _parse_headers(self, headers_str):
        headers = {}
        for line in headers_str.split("\r\n")[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return headers

    def _get_client_ip(self, headers, client):
        xff = headers.get("x-forwarded-for", "")
        if xff:
            first_ip = xff.split(",")[0].strip()
            if first_ip:
                return first_ip
        return client.getpeername()[0]

    def _relay(self, src, dst, stop_event):
        try:
            while not stop_event.is_set():
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            stop_event.set()
            try:
                dst.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadingTCPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    log(f"Listening on {LISTEN_HOST}:{LISTEN_PORT}, forwarding to {BACKEND_HOST}:{BACKEND_PORT} path={WS_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
