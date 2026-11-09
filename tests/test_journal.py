"""Проверка правил журнала. Запуск: python -m pytest   или   python tests/test_journal.py"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["OPERJOURNAL_DB"] = os.path.join(TMP, "test.db")
os.environ["OPERJOURNAL_ASR"] = "test"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_full_scenario():
    with TestClient(app) as c:
        # без смены записи вносить нельзя
        r = c.post("/api/entries", json={"text": "Запись без смены"})
        assert r.status_code == 400 and "Смена не принята" in r.json()["detail"]

        # неверный ПИН
        assert c.post("/api/shift/accept", json={"user_id": 1, "pin": "9999"}).status_code == 400
        assert c.post("/api/shift/accept", json={"user_id": 1, "pin": "1111"}).status_code == 200

        # голос (тестовый движок) -> разбор по справочнику и сокращениям
        r = c.post("/api/transcribe", files={"audio": ("a.webm", b"x", "audio/webm")}).json()
        assert "Q1" in r["parsed"]["text"] and "ЗН" in r["parsed"]["text"] and "кВ" in r["parsed"]["text"]
        assert r["parsed"]["kind"] == "switching"
        e = c.post("/api/entries", json={"text": r["parsed"]["text"], "kind": r["parsed"]["kind"], "raw_transcript": r["raw"]}).json()
        assert e["id"] == 2

        # неизвестная позиция -> предупреждение
        p = c.post("/api/parse", json={"text": "Датчик ХУ-77 показывает обрыв", "normalize": True}).json()
        assert any("ХУ-77" in w for w in p["warnings"])

        # пропущенная запись требует время события
        assert c.post("/api/entries", json={"text": "Течь масла Т1", "missed": True}).status_code == 400
        e = c.post("/api/entries", json={"text": "Течь масла Т1", "missed": True, "event_time": "13:05"}).json()
        assert e["text"].startswith("Пропущенная запись (время события 13:05)")

        # исправление — новой записью, повторно исправить нельзя
        e = c.post("/api/entries", json={"text": "Отключен выключатель Q2", "corrects_id": 2}).json()
        assert e["kind"] == "correction" and "Запись № 2 считать ошибочной" in e["text"]
        assert c.post("/api/entries", json={"text": "ещё", "corrects_id": 2}).status_code == 400

        # сдача смены: подписи обоих, чужую смену исправлять нельзя
        r = c.post("/api/shift/handover", json={"from_pin": "1111", "to_user_id": 2, "to_pin": "2222"})
        assert r.status_code == 200 and r.json()["cosigner_id"] == 2
        assert c.post("/api/entries", json={"text": "x", "corrects_id": 3}).status_code == 400

        rows = c.get("/api/entries").json()
        assert [x["id"] for x in rows] == [1, 2, 3, 4, 5]
        assert rows[1]["erroneous_by"] == 4

        assert c.get("/api/verify").json()["ok"] is True
        x = c.get("/api/export.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"

        # база не даёт изменить или удалить запись
        con = sqlite3.connect(os.environ["OPERJOURNAL_DB"])
        for sql in ("UPDATE entries SET text='x' WHERE id=2", "DELETE FROM entries WHERE id=2"):
            try:
                con.execute(sql)
                raise AssertionError("база позволила изменить запись")
            except sqlite3.DatabaseError:
                pass
        # подделка в обход программы обнаруживается
        con.execute("DROP TRIGGER entries_no_update")
        con.execute("UPDATE entries SET text='подделка' WHERE id=3")
        con.commit()
        con.close()
        v = c.get("/api/verify").json()
        assert v["ok"] is False and v["broken_at"] == 3

        # администрирование только по ПИН администратора
        assert c.post("/api/admin/login", headers={"X-Admin-Pin": "1111"}).status_code == 401
        assert c.post("/api/admin/login", headers={"X-Admin-Pin": "0000"}).status_code == 200


def test_document_check(tmp_path=None):
    from app import normcheck
    tmp = Path(tmp_path or TMP)
    body = "<h1>ПРАВИЛА</h1><p>(в ред. Приказа от 01.09.2023 N 714)</p>" + "".join(f"<p>Пункт {i}. Текст пункта правил.</p>" for i in range(60))
    f = tmp / "doc.html"
    f.write_text(body, encoding="utf-8")
    first = normcheck.check({"url": str(f)})
    assert first["status"] == "first" and first["redactions"]
    same = normcheck.check({"url": str(f), "saved_hash": first["pending_hash"], "saved_text": first["pending_text"]})
    assert same["status"] == "unchanged"
    f.write_text(body.replace("Пункт 7. Текст", "Пункт 7. Новый текст"), encoding="utf-8")
    changed = normcheck.check({"url": str(f), "saved_hash": first["pending_hash"], "saved_text": first["pending_text"]})
    assert changed["status"] == "changed" and any(d["op"] == "+" for d in changed["diff"])
    assert normcheck.check({"url": str(tmp / "нет.html")})["status"] == "error"


if __name__ == "__main__":
    test_full_scenario()
    test_document_check()
    print("Все проверки пройдены.")
