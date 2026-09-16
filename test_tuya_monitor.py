#!/usr/bin/env python3
"""Минимальные assert-проверки для tuya_monitor.py (без фреймворков)."""

from tuya_monitor import PhaseAnalyzer, apply_dps_to_device


def test_apply_dps_to_device():
    dev = {"voltage_v": 220.0, "current_a": 0.0, "power_w": 0.0,
           "temperature_c": 30, "state": "OFF", "rated_current_a": 16}
    dps = {
        "switch_1": True,
        "cur_voltage": 2199,   # 219.9 В
        "cur_current": 987,    # 0.987 А
        "cur_power": 1701,     # 170.1 Вт
        "temp_current": 39,
    }
    updated = apply_dps_to_device(dev, dps)
    assert updated is True
    assert dev["state"] == "ON"
    assert dev["voltage_v"] == 219.9
    assert dev["current_a"] == 0.987
    assert dev["power_w"] == 170.1
    assert dev["temperature_c"] == 39
    assert dev["load_percent"] == round(0.987 / 16 * 100, 1)


def test_apply_dps_to_device_no_known_codes():
    dev = {"voltage_v": 220.0, "current_a": 0.0}
    assert apply_dps_to_device(dev, {"unknown_code": 1}) is False
    assert dev["voltage_v"] == 220.0


def test_phase_analyzer_evaluate_detects_imbalance():
    schema = {
        "devices": [
            {"name": "A", "phase": "L1", "state": "ON", "category": "heating",
             "rated_current_a": 16, "current_a": 14.99, "power_w": 3107.0,
             "temperature_c": 39, "voltage_v": 206.9},
            {"name": "B", "phase": "L2", "state": "ON", "category": "rooms",
             "rated_current_a": 16, "current_a": 0.4, "power_w": 88.0,
             "temperature_c": 35, "voltage_v": 219.7},
            {"name": "C", "phase": "L3", "state": "ON", "category": "kitchen",
             "rated_current_a": 16, "current_a": 0.41, "power_w": 95.1,
             "temperature_c": 39, "voltage_v": 233.7},
        ]
    }
    analysis = PhaseAnalyzer.evaluate(schema)
    assert analysis["phase_summary"]["L1"]["total_current"] == 14.99
    assert analysis["voltage_spread_v"] > 25
    critical_alerts = [a for a in analysis["alerts"] if a["level"] == "CRITICAL"]
    assert any("Токовая перегрузка" in a["message"] for a in critical_alerts)


if __name__ == "__main__":
    test_apply_dps_to_device()
    test_apply_dps_to_device_no_known_codes()
    test_phase_analyzer_evaluate_detects_imbalance()
    print("OK: все проверки tuya_monitor.py прошли")
