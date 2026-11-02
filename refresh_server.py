#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальный HTTP-бэкенд для кнопки "Обновить" в дашборде.
POST /refresh запускает tuya_monitor.py --export и перезаписывает schema_data.json
свежими данными из Tuya Cloud. Ключи TUYA_API_KEY/TUYA_API_SECRET/TUYA_REGION
берутся из окружения процесса (systemd EnvironmentFile), в git не попадают.

POST /topology-save сохраняет структурные поля автоматов (фаза/рейка/позиция/щиток),
отредактированные в topology.html, прямо в schema_data.json — телеметрию не трогает,
её каждый час поверх всё равно перезатирает electro-daemon.
"""

import datetime
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "schema_data.json")
PORT = int(os.environ.get("REFRESH_PORT", "8877"))

# Поля, которые редактор топологии имеет право менять — всё остальное
# (телеметрию, имена, уставки, tuya_device_id, notes) трогать нельзя.
TOPOLOGY_EDITABLE_FIELDS = ("phase", "din_rail", "din_position", "location", "fed_by")


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/topology-save":
            self._handle_topology_save()
            return
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

    def _handle_topology_save(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 2_000_000:
            self._json(400, {"ok": False, "error": "empty or too large request body"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            edits = payload["devices"]
            if not isinstance(edits, list):
                raise ValueError("devices must be a list")
        except Exception as e:
            self._json(400, {"ok": False, "error": f"bad request body: {e}"})
            return

        try:
            with open(SCHEMA_PATH, encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"cannot read schema: {e}"})
            return

        by_id = {d.get("id"): d for d in schema.get("devices", [])}
        updated, skipped = 0, []
        for edit in edits:
            dev_id = edit.get("id")
            dev = by_id.get(dev_id)
            if dev is None:
                skipped.append(dev_id)
                continue
            for field in TOPOLOGY_EDITABLE_FIELDS:
                if field not in edit:
                    continue
                if edit[field] is None:
                    dev.pop(field, None)
                else:
                    dev[field] = edit[field]
            updated += 1

        # Бэкап перед перезаписью — на случай кривого дропа на фронте.
        backup_path = SCHEMA_PATH + ".bak-topology-" + datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        try:
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(schema, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # бэкап — best effort, не блокирует сохранение

        try:
            with open(SCHEMA_PATH, "w", encoding="utf-8") as f:
                json.dump(schema, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._json(500, {"ok": False, "error": f"cannot write schema: {e}"})
            return

        self._json(200, {"ok": True, "updated": updated, "skipped_unknown_ids": skipped})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
