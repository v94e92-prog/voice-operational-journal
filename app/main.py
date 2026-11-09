"""Веб-сервер прототипа «Оперативный журнал с голосовым вводом»."""
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import asr, db, export_xlsx, normcheck
from .parser import TextParser

BASE = db.BASE_DIR
DATA = db.DATA_DIR
STATIC = BASE / "static"


class State:
    config: dict = {}
    templates: dict = {}
    template_status: dict = {}
    journal: str = "oper_journal"
    parser: TextParser | None = None
    engine: asr.BaseEngine | None = None


S = State()


def init_app() -> None:
    S.config = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    if os.environ.get("OPERJOURNAL_ASR"):
        S.config["asr"]["engine"] = os.environ["OPERJOURNAL_ASR"]
    S.journal = S.config.get("journal_template", "oper_journal")

    docs = json.loads((DATA / "normative_docs.json").read_text(encoding="utf-8"))
    db.init_db(seed_docs=docs)

    S.templates, S.template_status = {}, {}
    for path in sorted((DATA / "templates").glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        body = json.loads(raw)
        S.templates[body["code"]] = body
        S.template_status[body["code"]] = db.register_template(body, raw)
    if S.journal not in S.templates:
        raise RuntimeError(f"Не найден шаблон {S.journal} в data/templates")

    S.parser = TextParser(DATA / "equipment.csv", DATA / "abbreviations.json")
    prompt = S.parser.vocabulary_prompt("Оперативный журнал. Наряд-допуск № 12. Отключен выключатель. Включены ЗН")
    S.engine = asr.create_engine(S.config.get("asr", {}), prompt)
    S.engine.start_loading()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_app()
    yield


app = FastAPI(title="Оперативный журнал — прототип", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(db.JournalError)
async def journal_error(request: Request, exc: db.JournalError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def template() -> dict:
    return S.templates[S.journal]


def require_admin(x_admin_pin: str | None = Header(default=None)) -> dict:
    user = db.find_admin_by_pin(x_admin_pin or "")
    if not user:
        raise HTTPException(status_code=401, detail="Нужен ПИН администратора.")
    return user


# ---------------------------------------------------------------- страницы

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC / "admin.html")


# ---------------------------------------------------------------- общее состояние

@app.get("/api/status")
def status():
    t = template()
    return {
        "organization": S.config.get("organization", ""),
        "department": S.config.get("department", ""),
        "journal": {"code": t["code"], "name": t["name"], "version": t["version"],
                    "effective_from": t.get("effective_from"), "kinds": t["entry_kinds"], "rules": t.get("rules", {})},
        "template_status": S.template_status.get(t["code"]),
        "shift": db.current_shift(S.journal),
        "asr": S.engine.info(),
    }


@app.post("/api/asr/retry")
def asr_retry():
    S.engine.start_loading()
    return S.engine.info()


@app.get("/api/users")
def users():
    return db.list_users()


# ---------------------------------------------------------------- смены

class AcceptIn(BaseModel):
    user_id: int
    pin: str


class HandoverIn(BaseModel):
    from_pin: str
    to_user_id: int
    to_pin: str
    note: str = ""


@app.post("/api/shift/accept")
def shift_accept(body: AcceptIn):
    return db.accept_shift(S.journal, template()["version"], body.user_id, body.pin)


@app.post("/api/shift/handover")
def shift_handover(body: HandoverIn):
    return db.handover_shift(S.journal, template()["version"], body.from_pin, body.to_user_id, body.to_pin, body.note)


# ---------------------------------------------------------------- голос и разбор

class ParseIn(BaseModel):
    text: str
    normalize: bool = False


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...)):
    if not S.engine.ready:
        raise HTTPException(status_code=503, detail=S.engine.info()["message"])
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
        f.write(audio.file.read())
        path = f.name
    try:
        raw = S.engine.transcribe(path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ошибка распознавания: {exc}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not raw.strip():
        raise HTTPException(status_code=422, detail="Речь не распознана. Говорите ближе к микрофону и повторите.")
    return {"raw": raw, "parsed": S.parser.parse(raw, normalize=True)}


@app.post("/api/parse")
def parse(body: ParseIn):
    return S.parser.parse(body.text, normalize=body.normalize)


# ---------------------------------------------------------------- записи

class EntryIn(BaseModel):
    text: str
    kind: str = "event"
    raw_transcript: str | None = None
    missed: bool = False
    event_time: str | None = None
    corrects_id: int | None = None
    pin: str | None = None


@app.get("/api/entries")
def entries(limit: int = 300, date_from: str | None = None, date_to: str | None = None):
    return db.list_entries(S.journal, limit=limit, date_from=date_from, date_to=date_to)


@app.post("/api/entries")
def add_entry(body: EntryIn):
    analysis = S.parser.parse(body.text, normalize=False)
    return db.add_entry(
        S.journal, template(), body.text, body.kind, analysis["fields"],
        raw_transcript=body.raw_transcript, missed=body.missed, event_time=body.event_time,
        corrects_id=body.corrects_id, pin=body.pin,
    )


@app.get("/api/verify")
def verify():
    return db.verify_chain()


@app.get("/api/export.xlsx")
def export(date_from: str | None = None, date_to: str | None = None):
    rows = db.list_entries(S.journal, limit=None, date_from=date_from, date_to=date_to)
    data = export_xlsx.build(rows, template(), S.config, date_from, date_to, db.verify_chain())
    name = f"operativny_zhurnal_{date.today():%Y-%m-%d}.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---------------------------------------------------------------- администрирование

class DocIn(BaseModel):
    url: str = ""
    start_marker: str = ""
    end_marker: str = ""


class UserIn(BaseModel):
    fio: str
    position: str = ""
    role: str = "operator"
    pin: str


@app.post("/api/admin/login")
def admin_login(admin: dict = Depends(require_admin)):
    return admin


@app.get("/api/admin/docs")
def admin_docs(admin: dict = Depends(require_admin)):
    return db.list_docs()


def _check_one(code: str) -> dict:
    doc = db.get_doc(code, with_text=True)
    result = normcheck.check(doc)
    db.save_check_result(code, result["status"], result["message"],
                         result.get("pending_hash"), result.get("pending_text"))
    result.pop("pending_text", None)
    result["code"] = code
    return result


@app.post("/api/admin/docs/{code}/check")
def admin_check(code: str, admin: dict = Depends(require_admin)):
    return _check_one(code)


@app.post("/api/admin/docs/check_all")
def admin_check_all(admin: dict = Depends(require_admin)):
    return [_check_one(d["code"]) for d in db.list_docs()]


@app.get("/api/admin/docs/{code}/diff")
def admin_diff(code: str, admin: dict = Depends(require_admin)):
    doc = db.get_doc(code, with_text=True)
    new = doc.get("pending_text") or ""
    return {
        "code": code,
        "status": doc.get("last_status"),
        "diff": normcheck.make_diff(doc.get("saved_text") or "", new) if doc.get("saved_text") and new else [],
        "redactions": normcheck.redactions(new or doc.get("saved_text") or ""),
        "preview": (new or doc.get("saved_text") or "")[:3000],
    }


@app.post("/api/admin/docs/{code}/accept")
def admin_accept(code: str, admin: dict = Depends(require_admin)):
    db.accept_pending(code, admin["fio"])
    return {"ok": True}


@app.post("/api/admin/docs/{code}")
def admin_update_doc(code: str, body: DocIn, admin: dict = Depends(require_admin)):
    db.update_doc(code, body.url, body.start_marker, body.end_marker, admin["fio"])
    return {"ok": True}


@app.get("/api/admin/templates")
def admin_templates(admin: dict = Depends(require_admin)):
    docs = {d["code"]: d["title"] for d in db.list_docs()}
    out = []
    for code, t in S.templates.items():
        out.append({**t, "status": S.template_status.get(code),
                    "basis_titles": [docs.get(c, c) for c in t.get("basis_docs", [])]})
    return {"templates": out, "versions": db.list_template_versions()}


@app.get("/api/admin/users")
def admin_users(admin: dict = Depends(require_admin)):
    return db.list_users(include_inactive=True)


@app.post("/api/admin/users")
def admin_add_user(body: UserIn, admin: dict = Depends(require_admin)):
    return {"id": db.add_user(body.fio, body.position, body.role, body.pin, admin["fio"])}


@app.post("/api/admin/backup")
def admin_backup(admin: dict = Depends(require_admin)):
    return db.make_backup(admin["fio"])


@app.get("/api/admin/backups")
def admin_backups(admin: dict = Depends(require_admin)):
    return db.list_backups()


@app.get("/api/admin/audit")
def admin_audit(admin: dict = Depends(require_admin)):
    return db.list_audit()
