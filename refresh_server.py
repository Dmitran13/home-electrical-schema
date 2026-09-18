#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный HTTP-бэкенд для кнопки "Обновить" в дашборде.
POST /refresh запускает tuya_monitor.py --export и перезаписывает schema_data.json
свежими данными из Tuya Cloud. Ключи TUYA_API_KEY/TUYA_API_SECRET/TUYA_REGION
берутся из окружения процесса (systemd EnvironmentFile), в git не попадают.
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "schema_data.json")
PORT = int(os.environ.get("REFRESH_PORT", "8877"))


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/refresh":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            result = subprocess.run(
                [
                    "python3", os.path.join(SCRIPT_DIR, "tuya_monitor.py"),
                    "--schema", SCHEMA_PATH,
                    "--export", SCHEMA_PATH,
                ],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "tuya_monitor.py timeout"})
            return

        if result.returncode != 0:
            self._json(502, {"ok": False, "error": result.stderr[-2000:] or result.stdout[-2000:]})
            return
        self._json(200, {"ok": True, "log": result.stdout[-2000:]})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
