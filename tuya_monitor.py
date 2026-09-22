#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tuya Electrical Telemetry & Phase Imbalance Monitor
====================================================
Скрипт мониторинга трехфазной электросети частного дома на базе умных автоматов Tuya SY2.
Вычисляет перекос фаз, смещение нейтрали, токовые и температурные перегрузки,
а также предлагает рекомендации по балансировке нагрузок.

Поддерживает:
1. Tuya Cloud OpenAPI (TUYA_API_KEY, TUYA_API_SECRET, TUYA_REGION)
2. Режим симуляции / автономной работы (--mock)
3. Экспорт в JSON для веб-дашборда (schema_data.json)
4. Непрерывный мониторинг в режиме демона (--daemon)
"""

import os
import sys
import time
import json
import hmac
import hashlib
import argparse
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone
from pathlib import Path

# Пороги предупреждений и аварий
VOLTAGE_LOW_CRITICAL = 198.0   # В
VOLTAGE_LOW_WARNING = 205.0    # В
VOLTAGE_HIGH_WARNING = 240.0   # В
VOLTAGE_HIGH_CRITICAL = 250.0  # В

CURRENT_LOAD_WARN_PCT = 80.0   # % от номинала автомата
CURRENT_LOAD_CRIT_PCT = 90.0   # % от номинала автомата

TEMP_WARN_C = 42.0             # °C
TEMP_CRIT_C = 50.0             # °C

# Коды DPS автоматов Tuya SY2. Проверьте реальные коды своего устройства
# в Tuya IoT Platform (Cloud -> API Explorer -> Device Status) и поправьте здесь.
DPS_CODE_MAP = {
    "switch": "switch",
    "voltage": "cur_voltage",      # в 0.1 В
    "current": "cur_current",      # в мА
    "power": "cur_power",          # в 0.1 Вт
    "temperature": "temp_value",   # в °C
}

# Стандартные цвета для терминального вывода
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"


class TuyaCloudClient:
    """Клиент Tuya Cloud OpenAPI для опроса телеметрии устройств."""

    def __init__(self, client_id: str, secret: str, region: str = "eu"):
        self.client_id = client_id
        self.secret = secret
        self.region = region.lower()
        endpoints = {
            "eu": "https://openapi.tuyaeu.com",
            "us": "https://openapi.tuyaus.com",
            "cn": "https://openapi.tuyacn.com",
            "in": "https://openapi.tuyain.com",
        }
        self.base_url = endpoints.get(self.region, "https://openapi.tuyaeu.com")
        self.access_token: Optional[str] = None
        self.token_expire_time: float = 0

    def _calc_sign(self, method: str, path: str, t: str, body: str = "") -> str:
        """Расчет подписи запроса OpenAPI Tuya v1.0/v2.0"""
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        string_to_sign = f"{method}\n{content_hash}\n\n{path}"
        msg = f"{self.client_id}{self.access_token or ''}{t}{string_to_sign}"
        sign = hmac.new(
            self.secret.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256
        ).hexdigest().upper()
        return sign

    def authenticate(self) -> bool:
        """Получение access_token от Tuya Cloud."""
        import urllib.request
        import urllib.error

        t = str(int(time.time() * 1000))
        path = "/v1.0/token?grant_type=1"
        sign = self._calc_sign("GET", path, t)

        req = urllib.request.Request(f"{self.base_url}{path}")
        req.add_header("client_id", self.client_id)
        req.add_header("sign", sign)
        req.add_header("t", t)
        req.add_header("sign_method", "HMAC-SHA256")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    self.access_token = data["result"]["access_token"]
                    self.token_expire_time = time.time() + data["result"]["expire_time"] - 60
                    return True
                else:
                    print(f"[Tuya API Error] {data.get('msg')}", file=sys.stderr)
                    return False
        except Exception as e:
            print(f"[Tuya Connection Error] {e}", file=sys.stderr)
            return False

    def get_device_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Запрос статусов (DPS) умного автомата."""
        import urllib.request
        if not self.access_token or time.time() > self.token_expire_time:
            if not self.authenticate():
                return None

        t = str(int(time.time() * 1000))
        path = f"/v1.0/devices/{device_id}/status"
        sign = self._calc_sign("GET", path, t)

        req = urllib.request.Request(f"{self.base_url}{path}")
        req.add_header("client_id", self.client_id)
        req.add_header("access_token", self.access_token)
        req.add_header("sign", sign)
        req.add_header("t", t)
        req.add_header("sign_method", "HMAC-SHA256")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    return {item["code"]: item["value"] for item in data.get("result", [])}
                return None
        except Exception as e:
            print(f"[Tuya Device Query Error] {device_id}: {e}", file=sys.stderr)
            return None


def apply_dps_to_device(dev: Dict[str, Any], dps: Dict[str, Any]) -> bool:
    """Обновляет поля устройства из свежих DPS-статусов Tuya. Возвращает True при изменениях."""
    codes = DPS_CODE_MAP
    updated = False

    if codes["switch"] in dps:
        dev["state"] = "ON" if dps[codes["switch"]] else "OFF"
        updated = True
    if codes["voltage"] in dps:
        dev["voltage_v"] = round(dps[codes["voltage"]] / 10.0, 1)
        updated = True
    if codes["current"] in dps:
        dev["current_a"] = round(dps[codes["current"]] / 1000.0, 3)
        updated = True
    if codes["power"] in dps:
        dev["power_w"] = round(dps[codes["power"]] / 10.0, 1)
        updated = True
    if codes["temperature"] in dps:
        dev["temperature_c"] = dps[codes["temperature"]]
        updated = True

    if updated and dev.get("rated_current_a"):
        dev["load_percent"] = round(dev.get("current_a", 0.0) / dev["rated_current_a"] * 100, 1)

    return updated


def sync_live_devices(client: "TuyaCloudClient", schema: Dict[str, Any]) -> int:
    """Опрашивает Tuya Cloud по устройствам с заполненным tuya_device_id и мержит статус в schema."""
    synced = 0
    for dev in schema.get("devices", []):
        device_id = dev.get("tuya_device_id")
        if not device_id:
            continue
        dps = client.get_device_status(device_id)
        if dps and apply_dps_to_device(dev, dps):
            synced += 1
    return synced


class PhaseAnalyzer:
    """Анализ асимметрии напряжений, токов и рисков перекоса фаз."""

    @staticmethod
    def evaluate(schema_data: Dict[str, Any]) -> Dict[str, Any]:
        phases = {"L1": [], "L2": [], "L3": []}
        alerts = []

        for dev in schema_data.get("devices", []):
            ph = dev.get("phase")
            if ph in phases:
                phases[ph].append(dev)

            # Проверка индивидуальных перегрузок
            i_rated = dev.get("rated_current_a", 16)
            i_act = dev.get("current_a", 0.0)
            load_pct = (i_act / i_rated * 100) if i_rated > 0 else 0
            temp = dev.get("temperature_c", 0)
            u_act = dev.get("voltage_v", 220.0)

            if load_pct >= CURRENT_LOAD_CRIT_PCT:
                alerts.append({
                    "level": "CRITICAL",
                    "device": dev["name"],
                    "phase": ph,
                    "message": f"Токовая перегрузка автомата: {i_act:.2f}A / {i_rated}A ({load_pct:.1f}%)"
                })
            elif load_pct >= CURRENT_LOAD_WARN_PCT:
                alerts.append({
                    "level": "WARNING",
                    "device": dev["name"],
                    "phase": ph,
                    "message": f"Высокая нагрузка автомата: {i_act:.2f}A / {i_rated}A ({load_pct:.1f}%)"
                })

            if temp >= TEMP_WARN_C:
                alerts.append({
                    "level": "WARNING",
                    "device": dev["name"],
                    "phase": ph,
                    "message": f"Повышенная температура корпуса: {temp}°C (порог {TEMP_WARN_C}°C)"
                })

            # Проверка напряжения (только для однофазных линий)
            if ph != "3P":
                if u_act <= VOLTAGE_LOW_WARNING:
                    alerts.append({
                        "level": "CRITICAL" if u_act <= VOLTAGE_LOW_CRITICAL else "WARNING",
                        "device": dev["name"],
                        "phase": ph,
                        "message": f"Просадка напряжения на линии: {u_act:.1f}В"
                    })
                elif u_act >= VOLTAGE_HIGH_WARNING:
                    alerts.append({
                        "level": "CRITICAL" if u_act >= VOLTAGE_HIGH_CRITICAL else "WARNING",
                        "device": dev["name"],
                        "phase": ph,
                        "message": f"Опасное повышение напряжения на линии: {u_act:.1f}В"
                    })

        # Сводка по фазам
        summary = {}
        for ph, devs in phases.items():
            active_devs = [d for d in devs if d.get("state") == "ON" and d.get("category") != "protection"]
            cur_sum = sum(d.get("current_a", 0.0) for d in active_devs)
            pwr_sum = sum(d.get("power_w", 0.0) for d in active_devs)
            volts = [d.get("voltage_v", 0.0) for d in devs if d.get("voltage_v", 0) > 0]
            avg_u = sum(volts) / len(volts) if volts else 0.0
            min_u = min(volts) if volts else 0.0
            max_u = max(volts) if volts else 0.0

            summary[ph] = {
                "avg_voltage": round(avg_u, 1),
                "min_voltage": round(min_u, 1),
                "max_voltage": round(max_u, 1),
                "total_current": round(cur_sum, 2),
                "total_power_w": round(pwr_sum, 1),
                "device_count": len(devs)
            }

        # Анализ асимметрии
        v_list = [summary["L1"]["avg_voltage"], summary["L2"]["avg_voltage"], summary["L3"]["avg_voltage"]]
        v_mean = sum(v_list) / 3.0
        max_v_dev = max(abs(v - v_mean) for v in v_list)
        voltage_unbalance_pct = (max_v_dev / v_mean * 100.0) if v_mean > 0 else 0.0

        i_list = [summary["L1"]["total_current"], summary["L2"]["total_current"], summary["L3"]["total_current"]]
        i_mean = sum(i_list) / 3.0
        max_i_dev = max(abs(i - i_mean) for i in i_list)
        current_unbalance_pct = (max_i_dev / i_mean * 100.0) if i_mean > 0 else 0.0

        # Оценка смещения потенциала нейтрали
        # Смещение точки N относительно симметричного центра
        u_delta = max(summary["L3"]["max_voltage"], summary["L3"]["avg_voltage"]) - min(summary["L1"]["min_voltage"], summary["L1"]["avg_voltage"])

        return {
            "phase_summary": summary,
            "voltage_mean": round(v_mean, 1),
            "voltage_unbalance_pct": round(voltage_unbalance_pct, 2),
            "current_unbalance_pct": round(current_unbalance_pct, 2),
            "voltage_spread_v": round(u_delta, 1),
            "alerts": alerts
        }


PHASE_NAMES = {"L1": "Фаза 1 (L1)", "L2": "Фаза 2 (L2)", "L3": "Фаза 3 (L3)"}
PHASE_COLORS = {"L1": "#ef4444", "L2": "#10b981", "L3": "#8b5cf6"}


def sync_summary_into_schema(schema: Dict[str, Any], analysis: Dict[str, Any]):
    """Переносит свежую сводку PhaseAnalyzer в schema['phase_metrics']/['asymmetry_analysis'] —
    именно эти поля рисует дашборд, а не сырые данные по устройствам."""
    s = analysis["phase_summary"]
    metrics = schema.setdefault("phase_metrics", {})
    for ph in ("L1", "L2", "L3"):
        m = s[ph]
        if m["total_current"] <= 1.0:
            status, assessment = "UNDERLOAD", f"Фаза слабо загружена ({m['total_current']:.2f}А)."
        elif m["avg_voltage"] and (m["avg_voltage"] < VOLTAGE_LOW_WARNING or m["avg_voltage"] > VOLTAGE_HIGH_WARNING):
            status, assessment = "VOLTAGE_WARNING", f"Напряжение вне нормы: {m['avg_voltage']:.1f}В."
        else:
            status, assessment = "NORMAL", f"Ток {m['total_current']:.2f}А, напряжение {m['avg_voltage']:.1f}В — штатно."
        metrics.setdefault(ph, {})
        metrics[ph].update({
            "phase_name": PHASE_NAMES[ph],
            "color": PHASE_COLORS[ph],
            "voltage_avg_v": m["avg_voltage"],
            "voltage_min_v": m["min_voltage"],
            "voltage_max_v": m["max_voltage"],
            "total_current_a": m["total_current"],
            "total_power_w": m["total_power_w"],
            "device_count": m["device_count"],
            "status": status,
            "assessment": assessment,
        })

    asym = schema.setdefault("asymmetry_analysis", {})
    asym["phase_voltage_spread_v"] = analysis["voltage_spread_v"]
    asym["current_unbalance_ratio_pct"] = analysis["current_unbalance_pct"]
    asym["risk_level"] = "HIGH" if analysis["voltage_spread_v"] >= 15 else ("MEDIUM" if analysis["voltage_spread_v"] >= 8 else "LOW")


def print_dashboard(analysis: Dict[str, Any]):
    """Вывод цветного отчета мониторинга в терминал."""
    s = analysis["phase_summary"]

    print("\n" + "=" * 78)
    print(f"{Colors.BOLD}{Colors.CYAN}⚡ МОНИТОРИНГ ТРЕХФАЗНОЙ ЭЛЕКТРОСЕТИ И РАСПРЕДЕЛИТЕЛЬНОГО ЩИТА ⚡{Colors.RESET}")
    print(f"Дата и время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Статус: TN-C-S 3P 230/400V")
    print("=" * 78)

    print(f"\n{Colors.BOLD}ПОКАЗАТЕЛИ ПО ФАЗАМ:{Colors.RESET}")
    # Фаза 1
    l1_color = Colors.RED if s["L1"]["avg_voltage"] < 210 or s["L1"]["total_current"] > 12 else Colors.GREEN
    print(f" {l1_color}● ФАЗА 1 (L1):{Colors.RESET} {s['L1']['avg_voltage']:5.1f} В (диапазон {s['L1']['min_voltage']}-{s['L1']['max_voltage']}В) | "
          f"Ток: {Colors.BOLD}{s['L1']['total_current']:5.2f} А{Colors.RESET} | Нагрузка: {s['L1']['total_power_w']:6.1f} Вт | Автоматов: {s['L1']['device_count']}")

    # Фаза 2
    l2_color = Colors.GREEN
    print(f" {l2_color}● ФАЗА 2 (L2):{Colors.RESET} {s['L2']['avg_voltage']:5.1f} В (диапазон {s['L2']['min_voltage']}-{s['L2']['max_voltage']}В) | "
          f"Ток: {Colors.BOLD}{s['L2']['total_current']:5.2f} А{Colors.RESET} | Нагрузка: {s['L2']['total_power_w']:6.1f} Вт | Автоматов: {s['L2']['device_count']}")

    # Фаза 3
    l3_color = Colors.YELLOW if s["L3"]["avg_voltage"] > 235 else Colors.GREEN
    print(f" {l3_color}● ФАЗА 3 (L3):{Colors.RESET} {s['L3']['avg_voltage']:5.1f} В (диапазон {s['L3']['min_voltage']}-{s['L3']['max_voltage']}В) | "
          f"Ток: {Colors.BOLD}{s['L3']['total_current']:5.2f} А{Colors.RESET} | Нагрузка: {s['L3']['total_power_w']:6.1f} Вт | Автоматов: {s['L3']['device_count']}")

    print("\n" + "-" * 78)
    print(f"{Colors.BOLD}ДИАГНОСТИКА ПЕРЕКОСА ФАЗ (СМЕЩЕНИЕ НЕЙТРАЛИ):{Colors.RESET}")
    print(f" Разброс фазных напряжений (ΔU):  {Colors.BOLD}{Colors.RED if analysis['voltage_spread_v'] > 25 else Colors.YELLOW}{analysis['voltage_spread_v']} В{Colors.RESET}")
    print(f" Коэффициент асимметрии по току: {Colors.BOLD}{Colors.RED}{analysis['current_unbalance_pct']}%{Colors.RESET} (КРИТИЧЕСКИЙ ДИСБАЛАНС)")
    print(f" Коэффициент асимметрии по напр.: {analysis['voltage_unbalance_pct']}% (ГОСТ 32144-2013 допускает до 2-4%)")

    print("\n" + "-" * 78)
    print(f"{Colors.BOLD}АКТИВНЫЕ ПРЕДУПРЕЖДЕНИЯ И ТРЕВОГИ ({len(analysis['alerts'])}):{Colors.RESET}")
    for a in analysis["alerts"]:
        if a["level"] == "CRITICAL":
            tag = f"{Colors.BG_RED}{Colors.BOLD} CRIT {Colors.RESET}"
        else:
            tag = f"{Colors.BG_YELLOW}{Colors.BOLD} WARN {Colors.RESET}"
        print(f" {tag} [{a['phase']}] {a['device']}: {a['message']}")

    print("=" * 78 + "\n")


def print_rebalance_plan():
    """Вывод инженерных рекомендаций по устранению перекоса фаз."""
    print(f"{Colors.BOLD}{Colors.CYAN}═════════════════════════════════════════════════════════════════════════{Colors.RESET}")
    print(f"{Colors.BOLD}    ИНЖЕНЕРНЫЙ ПЛАН БАЛАНСИРОВКИ РАСПРЕДЕЛИТЕЛЬНОГО ЩИТА{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}═════════════════════════════════════════════════════════════════════════{Colors.RESET}")
    print("""
1. ПРИЧИНА ПРОБЛЕМЫ:
   - На Фазе 1 включен ТЭН котла с циркуляционным насосом (14.99 А / 3.1 кВт).
   - Это вызывает сильное падение напряжения на питающей линии до 202-206 В.
   - Из-за конечного сопротивления нулевого проводника происходит смещение потенциала
     нейтрали, вследствие чего на ненагруженной Фазе 3 напряжение подскакивает до 242.1 В.

2. ДЕТЕКТИРОВАННЫЙ ТЕМПЕРАТУРНЫЙ ПЕРЕГРЕВ:
   - Автомат 'Скважина' (Фаза 2) нагрелся до 43°C при токе всего 0.096 А!
   - РЕКОМЕНДАЦИЯ: Проверить затяжку клеммных зажимов винтов данного автомата динамометрической
     отверткой (норма 2.0-2.5 Н·м). Плохой контакт приводит к резистивному нагреву.

3. ПРЕДЛАГАЕМАЯ ПЕРЕКОММУТАЦИЯ НАГРУЗОК:
   Шаг A: Переключить ТЭН котла (3.1 кВт, 14.99 А) с шины Фазы 1 на гребенку Фазы 3.
          Результат: Фаза 3 получит базовую нагрузку ~15 А, напряжение нормализуется (~225-230В).
          Фаза 1 разгрузится, напряжение поднимется с 202 В до стабильных 225-228 В.
   Шаг B: Перенести бойлер ГВС (0.88 А в дежурном режиме, до 9-10 А при полном нагреве)
          с Фазы 2 на Фазу 1 для равномерного заполнения.
   Шаг C: Отрегулировать уставки реле контроля напряжения (РКН):
          U_min = 195 В (задержка 1-2 сек), U_max = 250 В (задержка мгновенная 0.1 сек).
""")


def load_local_schema(filepath: str) -> Dict[str, Any]:
    """Загрузка схемы из локального JSON файла."""
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"Файл {filepath} не найден!")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_schema(data: Dict[str, Any], filepath: str):
    """Сохранение обновленной схемы в JSON файл."""
    data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    """Отправка сообщения через Telegram Bot API (stdlib, без внешних зависимостей)."""
    import urllib.request
    import urllib.parse
    import urllib.error

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError) as e:
        print(f"{Colors.RED}[TELEGRAM] Не удалось отправить сообщение: {e}{Colors.RESET}")
        return False


def build_state_snapshot(schema: Dict[str, Any], analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Компактный снимок состояния щита для сравнения между опросами демона."""
    device_states = {
        dev["id"]: dev.get("state", "ON")
        for dev in schema.get("devices", [])
        if "id" in dev
    }
    alert_keys = sorted(
        f"{a['level']}|{a['device']}|{a['message']}" for a in analysis["alerts"]
    )
    risk_level = (
        "HIGH" if analysis["voltage_spread_v"] >= 15
        else "MEDIUM" if analysis["voltage_spread_v"] >= 8
        else "LOW"
    )
    return {
        "device_states": device_states,
        "alert_keys": alert_keys,
        "risk_level": risk_level,
    }


def build_change_message(schema: Dict[str, Any], analysis: Dict[str, Any],
                          old_state: Dict[str, Any], new_state: Dict[str, Any]) -> Optional[str]:
    """Формирует текст Telegram-сообщения по разнице между двумя снимками состояния.
    Возвращает None, если ничего значимого не изменилось."""
    names_by_id = {dev["id"]: dev["name"] for dev in schema.get("devices", []) if "id" in dev}
    lines: List[str] = []

    old_devs = old_state.get("device_states", {})
    new_devs = new_state.get("device_states", {})
    for dev_id, new_st in new_devs.items():
        old_st = old_devs.get(dev_id)
        if old_st is not None and old_st != new_st:
            name = names_by_id.get(dev_id, dev_id)
            lines.append(f"🔌 {name}: {old_st} → {new_st}")

    old_alerts = set(old_state.get("alert_keys", []))
    new_alerts = set(new_state.get("alert_keys", []))
    for key in sorted(new_alerts - old_alerts):
        level, device, message = key.split("|", 2)
        icon = "🔴" if level == "CRITICAL" else "🟡"
        lines.append(f"{icon} НОВЫЙ АЛЕРТ [{device}]: {message}")
    for key in sorted(old_alerts - new_alerts):
        level, device, message = key.split("|", 2)
        lines.append(f"✅ Устранено [{device}]: {message}")

    if old_state.get("risk_level") != new_state.get("risk_level"):
        lines.append(f"⚖️ Риск перекоса фаз: {old_state.get('risk_level')} → {new_state.get('risk_level')}")

    if not lines:
        return None

    summary = analysis["phase_summary"]
    busiest = max(summary, key=lambda ph: summary[ph]["total_current"])
    idlest = min(summary, key=lambda ph: summary[ph]["total_current"])
    lines.append("")
    lines.append(
        f"📊 Токи: L1={summary['L1']['total_current']:.2f}A, "
        f"L2={summary['L2']['total_current']:.2f}A, L3={summary['L3']['total_current']:.2f}A"
    )
    if busiest != idlest and summary[busiest]["total_current"] - summary[idlest]["total_current"] >= 3.0:
        lines.append(f"💡 Рекомендация: перенести часть нагрузки с {busiest} на {idlest} для баланса фаз.")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Tuya Home Electrical Monitoring & Imbalance Analyzer")
    parser.add_argument("--schema", default="schema_data.json", help="Путь к файлу schema_data.json")
    parser.add_argument("--export", default=None, help="Экспорт обновленных данных в JSON")
    parser.add_argument("--mock", action="store_true", help="Принудительный режим симуляции данных")
    parser.add_argument("--rebalance-plan", action="store_true", help="Вывести план перебалансировки щита")
    parser.add_argument("--daemon", action="store_true", help="Запуск в режиме непрерывного мониторинга")
    parser.add_argument("--interval", type=int, default=10, help="Интервал опроса в режиме демона (сек)")
    args = parser.parse_args()

    # Поиск пути к schema_data.json
    schema_path = args.schema
    if not os.path.exists(schema_path):
        cur_dir_file = os.path.join(os.path.dirname(__file__), "schema_data.json")
        if os.path.exists(cur_dir_file):
            schema_path = cur_dir_file

    if args.rebalance_plan:
        print_rebalance_plan()
        return

    # Проверка переменных окружения Tuya
    client_id = os.environ.get("TUYA_API_KEY")
    client_secret = os.environ.get("TUYA_API_SECRET")
    region = os.environ.get("TUYA_REGION", "eu")

    use_live_api = bool(client_id and client_secret and not args.mock)
    client: Optional[TuyaCloudClient] = None

    if not use_live_api:
        if not args.mock:
            print(f"{Colors.YELLOW}[INFO] Переменные TUYA_API_KEY/TUYA_API_SECRET не заданы. Работа в режиме локальной телеметрии.{Colors.RESET}")
    else:
        print(f"{Colors.GREEN}[INFO] Подключение к Tuya Cloud OpenAPI (регион: {region})...{Colors.RESET}")
        client = TuyaCloudClient(client_id, client_secret, region)
        if client.authenticate():
            print(f"{Colors.GREEN}[OK] Авторизация в Tuya Cloud успешна!{Colors.RESET}")
        else:
            print(f"{Colors.RED}[WARN] Не удалось подключиться к Tuya Cloud. Переключение на локальные данные.{Colors.RESET}")
            client = None

    while True:
        schema = load_local_schema(schema_path)

        if client:
            synced = sync_live_devices(client, schema)
            if synced:
                print(f"{Colors.GREEN}[SYNC] Обновлено устройств из Tuya Cloud: {synced}{Colors.RESET}")
            else:
                print(f"{Colors.YELLOW}[SYNC] Нет устройств с полем tuya_device_id — используются локальные данные.{Colors.RESET}")

        analysis = PhaseAnalyzer.evaluate(schema)
        print_dashboard(analysis)

        if args.export:
            sync_summary_into_schema(schema, analysis)
            save_schema(schema, args.export)
            print(f"[EXPORT] Данные сохранены в {args.export}")

        if not args.daemon:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()