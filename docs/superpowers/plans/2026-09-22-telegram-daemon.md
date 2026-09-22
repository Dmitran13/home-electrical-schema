# Автономный демон диагностики щита с Telegram-алертами — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ СКИЛЛ: используйте superpowers:subagent-driven-development (рекомендуется) или superpowers:executing-plans для выполнения плана по задачам. Шаги отмечены чекбоксами (`- [ ]`) для отслеживания.

**Цель:** демон на сервере раз в час опрашивает Tuya Cloud, сравнивает состояние щита с прошлым опросом и, если что-то изменилось (автомат переключился, появился/пропал алерт, изменился риск перекоса фаз), шлёт сообщение в Telegram с диагнозом и рекомендацией — без ручных запросов.

**Архитектура:** расширение уже существующего `tuya_monitor.py --daemon` (сейчас есть, но не запущен как сервис). Новый набор чистых функций в `tuya_monitor.py`: снимок состояния (`build_state_snapshot`), сравнение снимков (`build_change_message`), отправка в Telegram (`send_telegram_message`, stdlib `urllib`, без новых зависимостей), персист снимка между опросами в JSON-файле рядом со `schema_data.json`. Новый systemd-юнит `electro-daemon.service` запускает демон с `--interval 3600`, токен и chat_id — в `.env` на сервере.

**Tech Stack:** Python 3 stdlib (`urllib.request`, `json`, `pathlib`), systemd, Telegram Bot API.

---

## Файловая структура

- **Modify:** `tuya_monitor.py` — новые функции + вызов из `main()` в теле daemon-цикла
- **Modify:** `test_tuya_monitor.py` — тесты на новые чистые функции
- **Create:** `deploy/electro-daemon.service` — systemd-юнит демона
- **Modify:** `deploy/deploy-electro.sh` — проверка `TELEGRAM_*` в `.env`, деплой нового юнита
- **Modify (на сервере, не в git):** `/opt/projects/home-electrical-schema/.env` — добавить `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

---

## Статус выполнения (обновлено контроллером в процессе)

- ✅ **Task 1** — `send_telegram_message` — коммит `be48377`
- ✅ **Task 2** — `build_state_snapshot` + `build_change_message` + тесты (TDD) — коммит `5a4c926`
- ✅ **Task 3** — `load_daemon_state` / `save_daemon_state` — коммит `5ff1f10`
- ✅ **Task 4** — wiring в `main()` daemon-цикл + `.gitignore` — коммит `50bfcff`
  - Code review нашёл Important-замечание (не блокер): при неудачной отправке в Telegram `save_daemon_state` всё равно перезаписывает state, из-за чего при двух подряд неудачных отправках первое изменение теряется из сравнения. Контроллер оценивает это как достаточно дешёвый фикс (2-строчное условие) относительно цели фичи ("не терять инциденты") — будет исправлено сразу после исходной задачи, отдельным маленьким коммитом.
- ⏳ **Task 5** — systemd-юнит — pending
- ⏳ **Task 6** — deploy-electro.sh — pending
- ⏳ **Task 7** — ключи на сервере + деплой + сквозной тест — pending

---

### Task 1: Функция отправки в Telegram

**Files:**
- Modify: `tuya_monitor.py` (добавить после `save_schema`, перед `def main():`)

- [x] **Step 1: Добавить функцию `send_telegram_message`**

```python
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
```

- [x] **Step 2:** Ручная проверка — не покрывается unit-тестом (сетевой I/O), проверяется сквозным тестом в Task 7.
- [x] **Step 3: Commit** `git commit -m "feat: добавить отправку сообщений в Telegram Bot API"`

---

### Task 2: Снимок состояния и сравнение снимков (TDD)

**Files:**
- Modify: `tuya_monitor.py`
- Test: `test_tuya_monitor.py`

- [x] Тесты: `test_build_state_snapshot`, `test_build_change_message_detects_breaker_toggle`, `test_build_change_message_no_change_returns_none`

- [x] **Реализация `build_state_snapshot`:**

```python
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
```

- [x] **Реализация `build_change_message`:**

```python
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
```

- [x] **Commit:** `git commit -m "feat: снимок состояния щита и диагностика изменений между опросами"`

Известное (некритичное) ограничение, найденное code review: если имя устройства содержит символ `|`, парсинг `alert_keys` (`split("|", 2)`) может неверно разделить device/message. В реальных данных имена автоматов `|` не содержат — не блокер, не фиксится в рамках этого плана.

---

### Task 3: Персист снимка между опросами демона

**Files:**
- Modify: `tuya_monitor.py` (сразу после `save_schema`, до `send_telegram_message`)

- [x] **Реализация:**

```python
def load_daemon_state(path: str) -> Optional[Dict[str, Any]]:
    """Загрузка снимка состояния предыдущего опроса демона. None, если снимка ещё нет."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_daemon_state(path: str, state: Dict[str, Any]):
    """Сохранение снимка состояния для сравнения на следующем опросе."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
```

- [x] **Commit:** `git commit -m "feat: персист снимка состояния демона между опросами"`

---

### Task 4: Подключить диагностику и Telegram-алерты в daemon-цикл

**Files:**
- Modify: `tuya_monitor.py` (функция `main()`)

- [x] Добавлено после блока чтения `TUYA_API_KEY`/`TUYA_API_SECRET`/`TUYA_REGION`:

```python
    # Проверка переменных окружения Telegram (опционально — без них диагностика просто не шлётся)
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (telegram_token and telegram_chat_id):
        print(f"{Colors.YELLOW}[INFO] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — уведомления отключены.{Colors.RESET}")

    state_path = os.path.join(os.path.dirname(os.path.abspath(schema_path)), ".electro_daemon_state.json")
```

- [x] Добавлено в конец тела `while True:`, после блока `args.export`, перед `if not args.daemon: break`:

```python
        new_state = build_state_snapshot(schema, analysis)
        old_state = load_daemon_state(state_path)
        if old_state is not None:
            message = build_change_message(schema, analysis, old_state, new_state)
            if message:
                print(f"{Colors.CYAN}[DIAGNOSIS] Обнаружены изменения:{Colors.RESET}\n{message}")
                if telegram_token and telegram_chat_id:
                    if send_telegram_message(telegram_token, telegram_chat_id, message):
                        print(f"{Colors.CYAN}[TELEGRAM] Уведомление отправлено{Colors.RESET}")
        save_daemon_state(state_path, new_state)

        if not args.daemon:
            break
        time.sleep(args.interval)
```

- [x] **Commit:** `git commit -m "feat: подключить диагностику изменений и Telegram-уведомления в daemon-цикл"` + добавлена строка `.electro_daemon_state.json` в `.gitignore` (runtime-артефакт, не для репозитория).

**Follow-up fix (после code review, отдельный коммит):** не сохранять `new_state`, если `send_telegram_message` вернул `False` — иначе при неудачной отправке изменение теряется из следующего сравнения и пользователь никогда о нём не узнает. Меняем безусловное `save_daemon_state(...)` на сохранение только когда: сообщения не было (nечего не изменилось), либо Telegram не настроен (тогда диагноз и так виден только в консоли), либо отправка прошла успешно.

**Побочный эффект (не баг, отмечено code review):** диагностика/отправка работает при ЛЮБОМ запуске `tuya_monitor.py --export ...`, включая одноразовый прогон через кнопку "Обновить" в дашборде — не только в `--daemon` режиме. Это осознанно (диагностика не должна зависеть от режима опроса).

---

### Task 5: systemd-юнит демона

**Files:**
- Create: `deploy/electro-daemon.service`

- [ ] **Step 1: Создать юнит по образцу `deploy/electro-refresh.service`**

```ini
[Unit]
Description=Home electrical schema — автономная диагностика щита (раз в час, Telegram-алерты)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/projects/home-electrical-schema
EnvironmentFile=/opt/projects/home-electrical-schema/.env
ExecStart=/usr/bin/python3 /opt/projects/home-electrical-schema/tuya_monitor.py --daemon --interval 3600 --schema /opt/projects/home-electrical-schema/schema_data.json --export /opt/projects/home-electrical-schema/schema_data.json
Restart=on-failure
RestartSec=30
User=www-data

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Commit** `git add deploy/electro-daemon.service && git commit -m "feat: systemd-юнит автономной диагностики щита"`

---

### Task 6: Деплой — проверка `.env`, установка юнита, добавление ключей на сервере

**Files:**
- Modify: `deploy/deploy-electro.sh:27-39` (добавить блок проверки `TELEGRAM_*`)
- Modify: `deploy/deploy-electro.sh:41-43` (добавить копирование `electro-daemon.service`)

- [ ] **Step 1:** После блока проверки `.env` для TUYA добавить:

```bash
echo "=== Ключи Telegram-бота (.env на сервере) ==="
ssh "$SERVER" "
  if ! grep -q '^TELEGRAM_BOT_TOKEN=' '$DEPLOY_DIR/.env' 2>/dev/null; then
    echo '  ВНИМАНИЕ: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не найдены в .env — добавьте вручную:'
    echo '  cat >> $DEPLOY_DIR/.env <<ENVEOF'
    echo '  TELEGRAM_BOT_TOKEN=...'
    echo '  TELEGRAM_CHAT_ID=...'
    echo '  ENVEOF'
  else
    echo '  TELEGRAM_BOT_TOKEN уже есть в .env — не трогаем'
  fi
"
```

- [ ] **Step 2:** После блока установки `electro-refresh.service` добавить:

```bash
echo "=== systemd-сервис автономной диагностики (electro-daemon) ==="
scp "$SCRIPT_DIR/electro-daemon.service" "$SERVER:/etc/systemd/system/electro-daemon.service"
ssh "$SERVER" "systemctl daemon-reload && systemctl enable --now electro-daemon"
```

- [ ] **Step 3: Commit** `git add deploy/deploy-electro.sh && git commit -m "feat: деплой автономного демона диагностики и проверка Telegram-ключей"`

---

### Task 7: Прописать Telegram-ключи на сервере и задеплоить

**Операционный шаг, выполняется напрямую (не через git), токен в git не попадает.**

- [ ] **Step 1:** Добавить `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` в `.env` на сервере (значения уже согласованы с пользователем в диалоге, контроллер знает их из контекста беседы).

- [ ] **Step 2:** `bash deploy/deploy-electro.sh` — деплой.

- [ ] **Step 3:** `systemctl status electro-daemon` — убедиться, что активен.

- [ ] **Step 4:** Сквозной тест — остановить демон, сбросить state-файл на сервере, один прогон (сеет baseline), попросить пользователя физически переключить автомат, второй прогон — убедиться что в Telegram пришло сообщение с именем автомата.

- [ ] **Step 5:** Запустить демон обратно (`systemctl start electro-daemon`).

---

## Самопроверка плана

- Все функции даны полным кодом, без TBD.
- Типы и имена функций согласованы между задачами.
- Триггер "только при изменении" — реализован через сравнение снимков, первый прогон не шлёт.
- Интервал — раз в час (`--interval 3600`).
- Ключи Telegram — не попадают в git.
