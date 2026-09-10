#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TCP Proxy with PROXY protocol v1 injection for Xray-core.
Supports IPv4 & IPv6 with WebSocket path sanitation and robust IP extraction.
"""

import ipaddress
import os
import socket
import socketserver
import threading

LISTEN_HOST = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PORT", "8080"))
BACKEND_HOST = os.environ.get("XRAY_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("XRAY_BACKEND_PORT", "5000"))
WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1")


def log(msg):
    print(f"[PROXY] {msg}", flush=True)


def normalize_path(path_str):
    """تطبيع مسار الويب سوكت لتفادي مشاكل الشرطات المائلة والبارامترات"""
    p = path_str.split("?")[0].strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") if p != "/" else "/"


def clean_ip_string(raw_str):
    """استخراج الآيبي النظيف سواء كان IPv4 أو IPv6 مع المنفذ أو بدونه"""
    raw = raw_str.strip()
    if not raw:
        return ""

    # 1. معالجة IPv6 المحصور بين أقواس مربعة مثل [2a03:2880::1]:8080 أو [2a03:2880::1]
    if raw.startswith("["):
        if "]" in raw:
            return raw[1:raw.index("]")]
        return raw.strip("[]")

    # 2. معالجة IPv4 الملتصق بمنفذ مثل 1.2.3.4:4567
    if "." in raw and ":" in raw:
        return raw.split(":")[0]

    return raw.strip("[]")


class ProxyHandler(socketserver.BaseRequestHandler):

    def handle(self):
        client = self.request
        client.settimeout(15)  # مهلة مخصصة لمصافحة الـ HTTP فقط
        backend = None
        try:
            data = self._read_headers(client)
            if not data:
                return

            headers_str = data.decode("latin-1", errors="ignore")
            method, path, _ = self._parse_request_line(headers_str)
            headers = self._parse_headers(headers_str)

            # التحقق من المسار والبروتوكول (الرد بـ 200 OK لفحص جاهزية Cloud Run)
            if normalize_path(path) != normalize_path(WS_PATH) or method.upper() != "GET":
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                client.sendall(response)
                return

            client_ip = self._get_client_ip(headers, client)

            try:
                client_port = client.getpeername()[1]
            except Exception:
                client_port = 12345

            # بناء ترويسة PROXY protocol v1 القياسية المتوافقة مع معيار HAProxy و Xray
            try:
                ip_obj = ipaddress.ip_address(client_ip)
                clean_ip = str(ip_obj)
                if ip_obj.version == 6:
                    proto = "TCP6"
                    dest_ip = "::1"
                else:
                    proto = "TCP4"
                    dest_ip = "127.0.0.1"
            except ValueError:
                proto = "TCP4"
                clean_ip = "127.0.0.1"
                dest_ip = "127.0.0.1"

            backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            backend.settimeout(15)
            backend.connect((BACKEND_HOST, BACKEND_PORT))

            # حقن ترويسة PROXY v1 متبوعة بالطلب الأصلي
            proxy_line = f"PROXY {proto} {clean_ip} {dest_ip} {client_port} {BACKEND_PORT}\r\n".encode("ascii")
            backend.sendall(proxy_line)
            backend.sendall(data)

            # إزالة المهلة الزمنية لضمان استمرار نفق الـ WebSocket مفتوحاً دون انقطاع
            client.settimeout(None)
            backend.settimeout(None)

            stop_event = threading.Event()
            t1 = threading.Thread(
                target=self._relay, args=(client, backend, stop_event), daemon=True
            )
            t2 = threading.Thread(
                target=self._relay, args=(backend, client, stop_event), daemon=True
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        except Exception as e:
            log(f"Connection error: {e}")
        finally:
            if backend:
                try:
                    backend.close()
                except Exception:
                    pass
            try:
                client.close()
            except Exception:
                pass

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
        parts = [p for p in lines[0].split() if p]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            return parts[0], parts[1], ""
        return "", "", ""

    def _parse_headers(self, headers_str):
        headers = {}
        for line in headers_str.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                key = k.strip().lower()
                val = v.strip()
                # دمج الحقول المتكررة (مثل X-Forwarded-For) وفق معيار RFC 7230
                if key in headers:
                    headers[key] = f"{headers[key]}, {val}"
                else:
                    headers[key] = val
        return headers

    def _get_client_ip(self, headers, client):
        # 1. فحص ترويسات البروكسي وشبكات الـ CDN المباشرة
        for header_name in ("cf-connecting-ip", "true-client-ip", "x-real-ip"):
            if header_name in headers:
                candidate = clean_ip_string(headers[header_name])
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    pass

        # 2. فحص ترويسة X-Forwarded-For بالترتيب العكسي
        xff = headers.get("x-forwarded-for", "")
        if xff:
            raw_ips = [item.strip() for item in xff.split(",") if item.strip()]
            for raw in reversed(raw_ips):
                clean = clean_ip_string(raw)
                try:
                    ip_obj = ipaddress.ip_address(clean)
                    # استبعاد العناوين الخاصة والمحلية التابعة لشبكة جوجل الداخلية
                    if not (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_unspecified):
                        return str(ip_obj)
                except ValueError:
                    continue

            # في حال كانت جميع العناوين خاصة، نأخذ أول عنوان من اليسار (أصل اتصال العميل)
            if raw_ips:
                fallback_candidate = clean_ip_string(raw_ips[0])
                return fallback_candidate

        # 3. الملاذ الأخير: قراءة مقبس الاتصال الداخلي
        try:
            return client.getpeername()[0]
        except Exception:
            return "127.0.0.1"

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
    log(f"Listening on {LISTEN_HOST}:{LISTEN_PORT}, backend at {BACKEND_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
