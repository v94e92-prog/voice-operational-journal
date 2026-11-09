"""Запуск прототипа: python run.py, затем откройте http://localhost:8000"""
import json
import threading
import webbrowser
from pathlib import Path

import uvicorn

BASE = Path(__file__).resolve().parent

if __name__ == "__main__":
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8000))
    url = f"http://localhost:{port}"
    print(f"\n  Оперативный журнал запущен: {url}\n  Администрирование: {url}/admin\n  Остановить: Ctrl+C\n")
    if cfg.get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")
