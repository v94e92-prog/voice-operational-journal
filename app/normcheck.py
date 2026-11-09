"""Проверка редакций нормативных документов. Без ИИ: скачать, вырезать текст,
сравнить с сохранённым. Решение о новой версии формы принимает человек.

Если на компьютере с программой нет интернета, сохраните страницу документа
на компьютере, где интернет есть, и укажите путь к файлу вместо ссылки:
  file:///C:/Документы/pravila.html   или просто   C:\\Документы\\pravila.html
"""
import difflib
import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import requests

MIN_TEXT_LEN = 500
_SKIP = {"script", "style", "noscript", "nav", "footer", "svg", "form", "button", "iframe", "select"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
          "table", "pre", "td", "th", "blockquote", "dd", "dt"}


class CheckError(Exception):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if "<" not in html[:2000]:
        raw = html
    else:
        p = _TextExtractor()
        p.feed(html)
        raw = "".join(p.parts)
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def fetch(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("file:"):
        path = Path(url2pathname(urlparse(url).path))
    elif re.match(r"^[A-Za-z]:[\\/]", url) or url.startswith("/"):
        path = Path(url)
    else:
        path = None
    if path is not None:
        if not path.exists():
            raise CheckError(f"Файл не найден: {path}")
        data = path.read_bytes()
        for enc in ("utf-8", "cp1251"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": "OperJournal/0.1 (проверка редакций нормативных документов)"})
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    return resp.text


def extract(html: str, start_marker: str = "", end_marker: str = "") -> str:
    text = html_to_text(html)
    if start_marker:
        i = text.find(start_marker)
        if i < 0:
            raise CheckError(f"На странице не найден начальный маркер «{start_marker}». Возможно, сайт изменил вёрстку.")
        text = text[i:]
    if end_marker:
        j = text.find(end_marker)
        if j < 0:
            raise CheckError(f"На странице не найден конечный маркер «{end_marker}». Возможно, сайт изменил вёрстку.")
        text = text[: j + len(end_marker)]
    return text


def redactions(text: str) -> list[str]:
    """Строки вида «(в ред. Приказа ... от ...)» — по ним видно, вносились ли изменения."""
    seen, out = set(), []
    for m in re.finditer(r"\(в ред\.[^()]{3,400}\)", text):
        s = re.sub(r"\s+", " ", m.group(0))
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:15]


def make_diff(old: str, new: str, limit: int = 400) -> list[dict]:
    out = []
    for line in difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=1):
        if line.startswith(("---", "+++")):
            continue
        op = "@" if line.startswith("@@") else line[:1]
        out.append({"op": op, "text": "…" if op == "@" else line[1:]})
        if len(out) >= limit:
            out.append({"op": "@", "text": f"… показаны первые {limit} строк изменений"})
            break
    return out


def check(doc: dict) -> dict:
    """Возвращает статус: no_url | error | first | unchanged | changed."""
    if not doc.get("url"):
        return {"status": "no_url", "message": "Не указана ссылка на документ или путь к файлу."}
    try:
        text = extract(fetch(doc["url"]), doc.get("start_marker", ""), doc.get("end_marker", ""))
    except CheckError as exc:
        return {"status": "error", "message": f"Проверить не удалось: {exc}"}
    except requests.RequestException as exc:
        return {"status": "error", "message": f"Проверить не удалось: сайт недоступен ({type(exc).__name__})."}
    if len(text) < MIN_TEXT_LEN:
        return {"status": "error", "message": "Проверить не удалось: на странице почти нет текста документа. "
                                              "Возможно, сайт изменил вёрстку или требует вход."}
    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result = {"pending_hash": new_hash, "pending_text": text, "redactions": redactions(text), "length": len(text)}
    if not doc.get("saved_hash"):
        result.update(status="first", message="Сохранённой редакции ещё нет. Просмотрите текст и нажмите «Принять как текущую».")
    elif new_hash == doc["saved_hash"]:
        result.update(status="unchanged", message="Изменений нет.", pending_hash=None, pending_text=None)
    else:
        result.update(status="changed", message="Текст документа изменился с момента принятой редакции. Проверьте изменения.",
                      diff=make_diff(doc.get("saved_text") or "", text))
    return result
