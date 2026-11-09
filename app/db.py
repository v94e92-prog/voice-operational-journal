"""Хранилище журнала.

Главные правила, которые здесь обеспечиваются:
* записи журнала нельзя изменить или удалить (запрет на уровне базы данных);
* у записей сквозная нумерация без пропусков;
* каждая запись связана с предыдущей хэшем (цепочка), поэтому правка базы
  в обход программы обнаруживается проверкой целостности;
* записи вносит только дежурный принятой смены;
* исправление — это новая запись, ошибочная остаётся читаемой.
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("OPERJOURNAL_DB", str(DATA_DIR / "journal.db")))
BACKUP_DIR = DB_PATH.parent / "backups"

GENESIS = "0" * 64
_write_lock = threading.Lock()


class JournalError(Exception):
    """Нарушение правил ведения журнала — показывается пользователю как есть."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fio TEXT NOT NULL,
    position TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'operator',
    pin_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shifts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journal TEXT NOT NULL,
    duty_user_id INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY,
    journal TEXT NOT NULL,
    template_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    shift_id INTEGER REFERENCES shifts(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    cosigner_id INTEGER REFERENCES users(id),
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',
    raw_transcript TEXT,
    corrects_id INTEGER REFERENCES entries(id),
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'Записи журнала нельзя изменять. Используйте исправление.'); END;

CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'Записи журнала нельзя удалять.'); END;

CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    who TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'Журнал действий нельзя изменять.'); END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'Журнал действий нельзя удалять.'); END;

CREATE TABLE IF NOT EXISTS template_versions(
    code TEXT NOT NULL,
    version TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY(code, version)
);

CREATE TABLE IF NOT EXISTS normative_docs(
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    start_marker TEXT NOT NULL DEFAULT '',
    end_marker TEXT NOT NULL DEFAULT '',
    saved_hash TEXT,
    saved_text TEXT,
    saved_at TEXT,
    saved_by TEXT,
    last_check_at TEXT,
    last_status TEXT,
    last_message TEXT,
    pending_hash TEXT,
    pending_text TEXT
);
"""

DEMO_USERS = [
    ("Иванов И. И.", "Электромонтёр по оперативным переключениям", "operator", "1111"),
    ("Петров П. П.", "Электромонтёр по оперативным переключениям", "operator", "2222"),
    ("Сидоров С. С.", "Ответственный за электрохозяйство", "admin", "0000"),
]


# ---------------------------------------------------------------- служебное

def now() -> str:
    """Местное время сервера с часовым поясом, до секунд."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    """Транзакция с блокировкой записи: записи получают номера строго по порядку."""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def check_pin(pin, stored: str) -> bool:
    if pin is None or not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    probe = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt.encode(), 100_000).hex()
    return secrets.compare_digest(probe, digest)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sentence(s: str) -> str:
    """Добавляет точку в конце, если её нет (ФИО «Иванов И. И.» уже с точкой)."""
    s = s.strip()
    return s if not s or s[-1] in ".!?" else s + "."


def write_audit(conn, who: str, action: str, details: str = "") -> None:
    conn.execute("INSERT INTO audit(at, who, action, details) VALUES(?,?,?,?)", (now(), who, action, details))


# ---------------------------------------------------------------- запуск

def init_db(seed_docs: list | None = None) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            for fio, position, role, pin in DEMO_USERS:
                conn.execute(
                    "INSERT INTO users(fio, position, role, pin_hash) VALUES(?,?,?,?)",
                    (fio, position, role, hash_pin(pin)),
                )
        for doc in seed_docs or []:
            conn.execute(
                "INSERT OR IGNORE INTO normative_docs(code, title, url, start_marker, end_marker) VALUES(?,?,?,?,?)",
                (doc["code"], doc["title"], doc.get("url", ""), doc.get("start_marker", ""), doc.get("end_marker", "")),
            )


# ---------------------------------------------------------------- пользователи

def list_users(include_inactive: bool = False) -> list[dict]:
    sql = "SELECT id, fio, position, role, active FROM users"
    if not include_inactive:
        sql += " WHERE active=1"
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY fio")]


def _user(conn, user_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if not row:
        raise JournalError("Работник не найден.")
    return row


def _check_user_pin(conn, user_id, pin) -> sqlite3.Row:
    user = _user(conn, user_id)
    if not check_pin(pin, user["pin_hash"]):
        raise JournalError(sentence(f"Неверный ПИН: {user['fio']}"))
    return user


def add_user(fio: str, position: str, role: str, pin: str, by: str) -> int:
    fio, position = fio.strip(), position.strip()
    if not fio:
        raise JournalError("Укажите ФИО.")
    if role not in ("operator", "admin"):
        raise JournalError("Неизвестная роль.")
    if not re.fullmatch(r"\d{4,8}", str(pin or "")):
        raise JournalError("ПИН должен состоять из 4–8 цифр.")
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO users(fio, position, role, pin_hash) VALUES(?,?,?,?)",
            (fio, position, role, hash_pin(pin)),
        )
        write_audit(conn, by, "Добавлен работник", f"{fio}, {position}, роль: {role}")
        return cur.lastrowid


def find_admin_by_pin(pin: str) -> dict | None:
    if not pin:
        return None
    with closing(connect()) as conn:
        for row in conn.execute("SELECT * FROM users WHERE role='admin' AND active=1"):
            if check_pin(pin, row["pin_hash"]):
                return {"id": row["id"], "fio": row["fio"], "position": row["position"]}
    return None


# ---------------------------------------------------------------- смены

def current_shift(journal: str) -> dict | None:
    with closing(connect()) as conn:
        return _current_shift(conn, journal)


def _current_shift(conn, journal: str) -> dict | None:
    row = conn.execute(
        """SELECT s.*, u.fio, u.position FROM shifts s JOIN users u ON u.id = s.duty_user_id
           WHERE s.journal=? AND s.ended_at IS NULL ORDER BY s.id DESC LIMIT 1""",
        (journal,),
    ).fetchone()
    return dict(row) if row else None


def accept_shift(journal: str, template_version: str, user_id: int, pin: str) -> dict:
    """Первый приём смены (когда открытой смены нет)."""
    with tx() as conn:
        if _current_shift(conn, journal):
            raise JournalError("Смена уже принята. Чтобы передать её, нажмите «Сдать смену».")
        user = _check_user_pin(conn, user_id, pin)
        cur = conn.execute(
            "INSERT INTO shifts(journal, duty_user_id, started_at) VALUES(?,?,?)", (journal, user["id"], now())
        )
        text = sentence(f"Смену принял {user['fio']}, {user['position'].lower()}" if user["position"] else f"Смену принял {user['fio']}")
        return _insert_entry(conn, {
            "journal": journal, "template_version": template_version, "created_at": now(),
            "shift_id": cur.lastrowid, "author_id": user["id"], "cosigner_id": None,
            "kind": "shift", "text": text, "fields": "{}", "raw_transcript": None, "corrects_id": None,
        })


def handover_shift(journal: str, template_version: str, from_pin: str, to_user_id: int, to_pin: str, note: str = "") -> dict:
    """Сдача смены: подписывают сдающий и принимающий, одна запись на двоих."""
    with tx() as conn:
        shift = _current_shift(conn, journal)
        if not shift:
            raise JournalError("Открытой смены нет — сначала примите смену.")
        giver = _check_user_pin(conn, shift["duty_user_id"], from_pin)
        if int(to_user_id) == giver["id"]:
            raise JournalError("Смену нельзя передать самому себе.")
        taker = _check_user_pin(conn, to_user_id, to_pin)
        text = sentence(f"Смену сдал {giver['fio']}") + " " + sentence(f"Смену принял {taker['fio']}")
        note = (note or "").strip()
        if note:
            text += " " + sentence(note)
        entry = _insert_entry(conn, {
            "journal": journal, "template_version": template_version, "created_at": now(),
            "shift_id": shift["id"], "author_id": giver["id"], "cosigner_id": taker["id"],
            "kind": "shift", "text": text, "fields": "{}", "raw_transcript": None, "corrects_id": None,
        })
        conn.execute("UPDATE shifts SET ended_at=? WHERE id=?", (now(), shift["id"]))
        conn.execute("INSERT INTO shifts(journal, duty_user_id, started_at) VALUES(?,?,?)", (journal, taker["id"], now()))
        return entry


# ---------------------------------------------------------------- записи

HASH_FIELDS = (
    "id", "journal", "template_version", "created_at", "shift_id", "author_id",
    "cosigner_id", "kind", "text", "fields", "corrects_id",
)


def compute_hash(prev_hash: str, rec: dict) -> str:
    payload = json.dumps({k: rec[k] for k in HASH_FIELDS}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}|{payload}".encode("utf-8")).hexdigest()


def _insert_entry(conn, rec: dict) -> dict:
    last = conn.execute("SELECT id, hash FROM entries ORDER BY id DESC LIMIT 1").fetchone()
    rec["id"] = last["id"] + 1 if last else 1
    rec["prev_hash"] = last["hash"] if last else GENESIS
    rec["hash"] = compute_hash(rec["prev_hash"], rec)
    cols = list(rec)
    conn.execute(
        f"INSERT INTO entries({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [rec[c] for c in cols],
    )
    return rec


def add_entry(journal: str, template: dict, text: str, kind: str, fields: dict,
              raw_transcript: str | None = None, missed: bool = False, event_time: str | None = None,
              corrects_id: int | None = None, pin: str | None = None) -> dict:
    rules = template.get("rules", {})
    allowed = {k["code"] for k in template["entry_kinds"] if not k.get("system")}
    text = sentence(re.sub(r"\s+", " ", (text or "")))
    if not text:
        raise JournalError("Текст записи пустой.")
    fields = dict(fields or {})

    with tx() as conn:
        shift = _current_shift(conn, journal)
        if not shift:
            raise JournalError("Смена не принята — записи вносить нельзя. Сначала примите смену.")
        if rules.get("require_pin_per_entry"):
            _check_user_pin(conn, shift["duty_user_id"], pin)

        if corrects_id:
            target = conn.execute("SELECT * FROM entries WHERE id=? AND journal=?", (corrects_id, journal)).fetchone()
            if not target:
                raise JournalError(f"Запись № {corrects_id} не найдена.")
            if target["kind"] == "shift":
                raise JournalError("Записи о приёме и сдаче смены исправлять нельзя.")
            done = conn.execute("SELECT id FROM entries WHERE corrects_id=?", (corrects_id,)).fetchone()
            if done:
                raise JournalError(f"Запись № {corrects_id} уже исправлена записью № {done['id']}.")
            if rules.get("correction_only_current_shift", True) and target["shift_id"] != shift["id"]:
                raise JournalError(
                    "Исправлять можно только записи своей смены. "
                    "Для более ранних записей обратитесь к ответственному за электрохозяйство."
                )
            kind = "correction"
            if len(text) > 1 and text[0].isupper() and text[1].islower():
                text = text[0].lower() + text[1:]
            text = f"Запись № {corrects_id} считать ошибочной. Правильно: {text}"
        elif kind not in allowed:
            raise JournalError("Неизвестный вид записи.")

        if missed:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", event_time or ""):
                raise JournalError("Для пропущенной записи укажите время события в формате ЧЧ:ММ.")
            fields.update({"missed": True, "event_time": event_time})
            text = f"Пропущенная запись (время события {event_time}). {text}"

        return _insert_entry(conn, {
            "journal": journal, "template_version": template["version"], "created_at": now(),
            "shift_id": shift["id"], "author_id": shift["duty_user_id"], "cosigner_id": None,
            "kind": kind, "text": text, "fields": json.dumps(fields, ensure_ascii=False, sort_keys=True),
            "raw_transcript": raw_transcript, "corrects_id": corrects_id,
        })


def list_entries(journal: str, limit: int | None = 300, date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    where, args = ["e.journal=?"], [journal]
    if date_from:
        where.append("substr(e.created_at,1,10) >= ?")
        args.append(date_from)
    if date_to:
        where.append("substr(e.created_at,1,10) <= ?")
        args.append(date_to)
    sql = f"""SELECT e.*, a.fio AS author_fio, a.position AS author_position, c.fio AS cosigner_fio
              FROM entries e JOIN users a ON a.id=e.author_id LEFT JOIN users c ON c.id=e.cosigner_id
              WHERE {' AND '.join(where)} ORDER BY e.id DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    with closing(connect()) as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
        corrected = {r["corrects_id"]: r["id"] for r in conn.execute(
            "SELECT id, corrects_id FROM entries WHERE corrects_id IS NOT NULL")}
    rows.reverse()
    for r in rows:
        r["fields"] = json.loads(r["fields"] or "{}")
        r["erroneous_by"] = corrected.get(r["id"])
    return rows


def verify_chain() -> dict:
    """Пересчитывает цепочку хэшей и сквозную нумерацию."""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM entries ORDER BY id").fetchall()
    prev, expected = GENESIS, 1
    for row in rows:
        rec = dict(row)
        if rec["id"] != expected:
            return {"ok": False, "count": len(rows), "broken_at": rec["id"],
                    "reason": f"Нарушена сквозная нумерация: после записи № {expected - 1} идёт № {rec['id']}."}
        if rec["prev_hash"] != prev or compute_hash(prev, rec) != rec["hash"]:
            return {"ok": False, "count": len(rows), "broken_at": rec["id"],
                    "reason": f"Запись № {rec['id']} изменена в обход программы."}
        prev, expected = rec["hash"], expected + 1
    return {"ok": True, "count": len(rows), "last_hash": prev}


# ---------------------------------------------------------------- шаблоны форм

def register_template(body: dict, raw_text: str) -> str:
    """Регистрирует версию шаблона. Возвращает ok / registered / modified."""
    body_hash = sha256_text(raw_text)
    with tx() as conn:
        row = conn.execute(
            "SELECT body_hash FROM template_versions WHERE code=? AND version=?", (body["code"], body["version"])
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO template_versions VALUES(?,?,?,?,?,?,?)",
                (body["code"], body["version"], body.get("effective_from", ""), now(),
                 "загрузка при запуске", body_hash, raw_text),
            )
            write_audit(conn, "система", "Зарегистрирована версия формы", f"{body['name']} v{body['version']}")
            return "registered"
        return "ok" if row["body_hash"] == body_hash else "modified"


def list_template_versions() -> list[dict]:
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT code, version, effective_from, registered_at, registered_by FROM template_versions ORDER BY code, registered_at")]


# ---------------------------------------------------------------- нормативные документы

DOC_PUBLIC = ("code, title, url, start_marker, end_marker, saved_hash, saved_at, saved_by, last_check_at, "
              "last_status, last_message, pending_hash, length(saved_text) AS saved_len, length(pending_text) AS pending_len")


def list_docs() -> list[dict]:
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute(f"SELECT {DOC_PUBLIC} FROM normative_docs ORDER BY code")]


def get_doc(code: str, with_text: bool = False) -> dict:
    cols = "*" if with_text else DOC_PUBLIC
    with closing(connect()) as conn:
        row = conn.execute(f"SELECT {cols} FROM normative_docs WHERE code=?", (code,)).fetchone()
    if not row:
        raise JournalError("Документ не найден.")
    return dict(row)


def update_doc(code: str, url: str, start_marker: str, end_marker: str, by: str) -> None:
    with tx() as conn:
        cur = conn.execute(
            "UPDATE normative_docs SET url=?, start_marker=?, end_marker=? WHERE code=?",
            (url.strip(), start_marker.strip(), end_marker.strip(), code),
        )
        if not cur.rowcount:
            raise JournalError("Документ не найден.")
        write_audit(conn, by, "Изменены настройки проверки документа", f"{code}: {url.strip()}")


def save_check_result(code: str, status: str, message: str, pending_hash=None, pending_text=None) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE normative_docs SET last_check_at=?, last_status=?, last_message=?, pending_hash=?, pending_text=? WHERE code=?",
            (now(), status, message, pending_hash, pending_text, code),
        )


def accept_pending(code: str, by: str) -> None:
    with tx() as conn:
        row = conn.execute("SELECT title, pending_hash, pending_text FROM normative_docs WHERE code=?", (code,)).fetchone()
        if not row or not row["pending_hash"]:
            raise JournalError("Нет новой редакции для принятия — сначала выполните проверку.")
        conn.execute(
            """UPDATE normative_docs SET saved_hash=pending_hash, saved_text=pending_text, saved_at=?, saved_by=?,
               pending_hash=NULL, pending_text=NULL, last_status='unchanged',
               last_message='Принята как текущая редакция' WHERE code=?""",
            (now(), by, code),
        )
        write_audit(conn, by, "Принята текущая редакция документа", row["title"])


# ---------------------------------------------------------------- резервные копии и журнал действий

def make_backup(by: str) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"journal_{datetime.now():%Y%m%d_%H%M%S}.db"
    with _write_lock:
        src = connect()
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    with tx() as conn:
        write_audit(conn, by, "Создана резервная копия", path.name)
    return {"name": path.name, "size": path.stat().st_size}


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("journal_*.db"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size,
             "created": datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat(timespec="seconds")}
            for f in files]


def list_audit(limit: int = 100) -> list[dict]:
    with closing(connect()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]
