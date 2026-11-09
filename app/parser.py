"""Разбор продиктованного текста.

1. Находит оборудование по справочнику (позиции и варианты произношения)
   и заменяет услышанное на правильное обозначение: «ку один» -> Q1.
2. Применяет перечень сокращений предприятия: «заземляющие ножи» -> ЗН.
3. Определяет вид записи, номера нарядов и бланков, время события.
4. Предупреждает о позициях, которых нет в справочнике.

Решение всегда за человеком: программа только предлагает текст.
"""
import csv
import json
import re
from pathlib import Path

from rapidfuzz import fuzz

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
    "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ъ": "", "ь": "",
}

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё\-./]*")
_TAG_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-яЁё])([A-ZА-ЯЁ]{1,4})([\s\-]?)(\d{1,4}[A-ZА-ЯЁа-яa-z]?)(?![0-9A-Za-zА-Яа-яЁё])")

KIND_RULES = [
    ("work", re.compile(r"наряд|допуск|допущен|бригад|по распоряжению\s*(№|номер)|работы? в порядке текущей эксплуатации")),
    ("switching", re.compile(r"отключ|включ|заземл|разземл|переключ|вывед|выведен|введ[её]н в работу|выкат|вкат|"
                             r"бланк[а-я]* переключ|сня[тл][аоы]? .{0,30}(зн|пз|заземл)|установлен[аоы]? .{0,30}(пз|заземл)")),
    ("order", re.compile(r"команд|распоряжени|разрешени|указани|сообщил|доложил|получил[аи]? разрешение")),
]


def norm_code(s: str) -> str:
    """Обозначение позиции к единому виду: регистр, алфавит, без пробелов и дефисов."""
    s = s.lower()
    out = []
    for ch in s:
        if ch in _CYR_TO_LAT:
            out.append(_CYR_TO_LAT[ch])
        elif ch.isalnum():
            out.append(ch)
    return "".join(out)


def norm_phrase(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class TextParser:
    def __init__(self, equipment_csv: Path, abbreviations_json: Path):
        self.equipment = []
        with open(equipment_csv, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f, delimiter=";")
            next(reader, None)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                code = row[0].strip()
                name = row[1].strip() if len(row) > 1 else ""
                aliases = [a.strip() for a in (row[2] if len(row) > 2 else "").split("|") if a.strip()]
                self.equipment.append({
                    "code": code, "name": name, "aliases": aliases,
                    "code_norm": norm_code(code),
                    "alias_norm": [norm_phrase(a) for a in aliases],
                })
        self.by_code = {e["code_norm"]: e for e in self.equipment}
        data = json.loads(Path(abbreviations_json).read_text(encoding="utf-8"))
        items = sorted(data.get("items", []), key=lambda p: -len(p[0]))
        self.abbreviations = [
            (re.compile(r"(?<![\wЁё])" + re.escape(src) + r"(?![\wЁё])", re.IGNORECASE), dst) for src, dst in items
        ]

    # ------------------------------------------------------------ подсказка для распознавания
    def vocabulary_prompt(self, extra: str = "") -> str:
        """Текст-подсказка для Whisper: модель охотнее пишет термины, которые видела в подсказке."""
        parts = [extra] if extra else []
        for e in self.equipment:
            parts.append(f"{e['name']} {e['code']}")
        return ". ".join(parts)[:700]

    # ------------------------------------------------------------ поиск оборудования
    def _find_equipment(self, text: str) -> list[dict]:
        tokens = list(_TOKEN_RE.finditer(text))
        used = [False] * len(tokens)
        found = []
        for n in (4, 3, 2, 1):
            for i in range(len(tokens) - n + 1):
                if any(used[i:i + n]):
                    continue
                start, end = tokens[i].start(), tokens[i + n - 1].end()
                piece = text[start:end]
                match = None
                if n <= 2:
                    match = self.by_code.get(norm_code(piece))
                    if match and n == 2 and not any(ch.isdigit() for ch in piece):
                        match = None
                if not match:
                    phrase = norm_phrase(piece)
                    best, best_score = None, 0
                    for e in self.equipment:
                        for alias in e["alias_norm"]:
                            if len(alias.split()) != n:
                                continue
                            score = fuzz.ratio(phrase, alias)
                            if score > best_score:
                                best, best_score = e, score
                    if best and best_score >= 88:
                        match = best
                if match:
                    for k in range(i, i + n):
                        used[k] = True
                    found.append({"code": match["code"], "name": match["name"], "start": start, "end": end, "heard": piece})
        found.sort(key=lambda m: m["start"])
        return found

    # ------------------------------------------------------------ основной разбор
    def parse(self, text: str, normalize: bool = True) -> dict:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        replacements = []

        equipment = self._find_equipment(text)
        if normalize and equipment:
            out, pos = [], 0
            for m in equipment:
                out.append(text[pos:m["start"]])
                out.append(m["code"])
                if m["heard"] != m["code"]:
                    replacements.append({"from": m["heard"], "to": m["code"]})
                pos = m["end"]
            out.append(text[pos:])
            text = "".join(out)

        if normalize:
            for pattern, dst in self.abbreviations:
                def _sub(mt, dst=dst):
                    replacements.append({"from": mt.group(0), "to": dst})
                    return dst
                text = pattern.sub(_sub, text)
            text = re.sub(r"(?i)\bномер\s+(?=\d)", "№ ", text)
            text = re.sub(r"\s+([,.;:])", r"\1", text)
            if text:
                text = text[0].upper() + text[1:]
                if text[-1] not in ".!?":
                    text += "."

        low = text.lower()
        kind = "event"
        for code, rx in KIND_RULES:
            if rx.search(low):
                kind = code
                break

        fields = {}
        m = re.search(r"наряд[а-я\-]*(?:\s*-?\s*допуск[а-я]*)?\s*(?:№|номер|n)\s*(\d+)", low)
        if m:
            fields["naryad"] = m.group(1)
        m = re.search(r"распоряжени[а-я]*\s*(?:№|номер)\s*(\d+)", low)
        if m:
            fields["order_no"] = m.group(1)
        m = re.search(r"бланк[а-я]*\s+переключени[а-я]*\s*(?:№|номер)\s*(\d+)", low)
        if m:
            fields["blank"] = m.group(1)
        m = re.search(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)", low) or \
            re.search(r"(?<!\d)([01]?\d|2[0-3]) час[а-я]*(?: (\d{1,2}) минут[а-я]*)?", low)
        if m:
            fields["time_mentioned"] = f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"

        known = {norm_code(e["code"]) for e in self.equipment}
        equip_out, seen = [], set()
        for e in self._find_equipment(text) if normalize else equipment:
            if e["code"] not in seen:
                seen.add(e["code"])
                equip_out.append({"code": e["code"], "name": e["name"]})

        warnings = []
        for t in _TAG_RE.finditer(text):
            letters, sep, digits = t.groups()
            if len(letters) == 1 and sep == " ":
                continue  # «В 14:20», «С 2 секции» и т. п.
            if norm_code(letters + digits) not in known:
                warnings.append(f"«{t.group(0)}» нет в справочнике оборудования — проверьте обозначение.")
        if "смену сдал" in low or "смену принял" in low:
            warnings.append("Приём и сдачу смены оформляйте кнопкой «Сдать смену» — там подписи обоих.")

        fields["equipment"] = equip_out
        return {"text": text, "kind": kind, "fields": fields, "warnings": warnings, "replacements": replacements}
