"""NOWA Registrar · check — deterministic validation of a citizen / Planet block.

The same code runs at the bot, at a member's ambassador and at the Star.
No AI here: code decides structure and red flags; humans decide acceptance.
Standard library only. Questionnaires (fields, limits, wording) live in ../questionnaires/*.toml.
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

QDIR = Path(__file__).resolve().parent.parent / "questionnaires"

# Red flags. A flag never rejects by itself: it is shown to the person (to fix) and to reviewers.
FLAGS: dict[str, list[str]] = {
    "secret": [
        r"(?i)\bsk-[A-Za-z0-9_\-]{16,}", r"\bgh[pousr]_[A-Za-z0-9]{20,}", r"\bgithub_pat_\w{20,}",
        r"\bgsk_[A-Za-z0-9]{16,}", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_\-]{30,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"(?i)(password|пароль|api[_-]?key|token|токен)\s*[:=]\s*\S{6,}",
    ],
    "infrastructure": [
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b", r"(?i)(?:^|\s)(/home/|/root/|/var/|/etc/|/opt/|[A-Z]:\\)",
        r"(?i)\b[a-z0-9-]+\.(internal|local|lan)\b",
    ],
    "personal": [
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", r"(?<!\d)\+?\d[\d\-\s().]{8,}\d",
        r"(?i)\b(дата рождения|date of birth|SSN|паспорт|passport)\b", r"(?i)\bt\.me/\S+", r"(?<!\w)@[A-Za-z0-9_]{4,}",
    ],
    "money": [r"(?i)[$€£₽]\s?\d", r"(?i)\b\d[\d.,]*\s?(k|m|млн|тыс|million|thousand)\b", r"(?i)\b(выручк|оборот|revenue|прибыл|profit)\w*"],
    "sensitive_topic": [
        r"(?i)\b(маржа|маржинальн|margin|себестоимост|COGS|закупочн\w* цен|wholesale|ставк\w* (зарплат|найма)|"
        r"пациент|patient|диагноз|diagnos|HIPAA|NDA)\b",
    ],
    "agent_instruction": [
        r"(?i)\b(ignore|disregard) (all|previous|your|the) (rules|instructions)",
        r"(?i)игнорируй (свои |все |предыдущие )?(правила|инструкции)",
        r"(?i)\b(you must|ты обязан|выполни команду|system prompt|одобри заявку|approve this)\b", r"<!--", r"<script",
    ],
}

FLAG_TEXT = {
    "secret": "похоже на ключ или пароль",
    "infrastructure": "адрес сервера или путь",
    "personal": "контакт или личные данные (контакт — сам Telegram)",
    "money": "суммы или финансовые показатели",
    "sensitive_topic": "маржа, цены, медицина или NDA",
    "agent_instruction": "текст похож на команду для ИИ",
}


@dataclass
class Questionnaire:
    kind: str
    level: int
    version: int
    header: str
    section: str
    max_chars: int
    fields: list[dict]
    purpose: str = ""
    visible_to: str = ""

    @property
    def labels(self) -> list[str]:
        return [f["label"] for f in self.fields]


def load(kind: str, level: int = 1) -> Questionnaire:
    data = tomllib.loads((QDIR / f"{kind}_L{level}.toml").read_text(encoding="utf-8"))
    return Questionnaire(
        kind=data["kind"], level=data["level"], version=data["version"], header=data["header"],
        section=data["section"], max_chars=data["max_chars"], fields=data["field"],
        purpose=data.get("purpose", ""), visible_to=data.get("visible_to", ""),
    )


def all_questionnaires() -> list[Questionnaire]:
    out = []
    for p in sorted(QDIR.glob("*_L*.toml")):
        kind, lvl = p.stem.rsplit("_L", 1)
        out.append(load(kind, int(lvl)))
    return out


@dataclass
class Result:
    kind: str | None = None
    version: int | None = None
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    sha256: str = ""
    text: str = ""

    def public(self) -> dict:
        """What may be shown to the person and logged: no field values, no matched secret text."""
        return {"kind": self.kind, "version": self.version, "ok": self.ok, "errors": self.errors,
                "warnings": self.warnings, "flags": {k: FLAG_TEXT[k] for k in self.flags}, "sha256": self.sha256}


def extract(text: str) -> str:
    """People paste blocks with ``` fences or chat text around: keep from the NOWA header to the end of the block."""
    m = re.search(r"^# NOWA (citizen|planet) L\d v\d+.*?(?=^```|\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else text.strip()


def render(q: Questionnaire, values: dict[str, str]) -> str:
    lines = [q.header, "", f"## {q.section}"]
    for f in q.fields:
        lines.append(f"- {f['label']}: {values.get(f['label'], '').strip() or 'не указано'}")
    return "\n".join(lines)


def _parse_fields(block: str) -> tuple[dict[str, str], list[str]]:
    out, dup = {}, []
    for m in re.finditer(r"^\s*-\s*([^:\n]+?)\s*:\s*(.*)$", block, re.M):
        k = m.group(1).strip()
        if k in out:
            dup.append(k)
        out[k] = m.group(2).strip()
    return out, dup


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def scan(text: str) -> dict[str, str]:
    hits = {}
    for name, pats in FLAGS.items():
        for p in pats:
            m = re.search(p, text)
            if m:
                hits[name] = m.group(0)[:40]
                break
    return hits


def check(raw: str) -> Result:
    text = extract(raw)
    res = Result(text=text)
    q = next((x for x in all_questionnaires() if text.startswith(x.header)), None)
    if q is None:
        res.errors.append("первая строка должна быть «# NOWA citizen L1 v1» или «# NOWA planet L1 v1»")
        return res
    res.kind, res.version = q.kind, q.version
    if len(text) > q.max_chars:
        res.errors.append(f"слишком длинно: {len(text)} > {q.max_chars} знаков")
    sec = _sections(text)
    if q.section not in sec:
        res.errors.append(f"нет раздела ## {q.section}")
    extra = [s for s in sec if s != q.section]
    if extra:
        res.errors.append("лишние разделы: " + ", ".join(extra))
    values, dup = _parse_fields(sec.get(q.section, ""))
    if dup:
        res.errors.append("поле повторяется: " + ", ".join(dup))
    unknown = [k for k in values if k not in q.labels]
    if unknown:
        res.errors.append("неизвестные поля: " + ", ".join(unknown))
    for f in q.fields:
        v = values.get(f["label"], "")
        empty = not v or v.lower() in ("не указано", "-", "—")
        if f.get("required") and empty:
            res.warnings.append(f"пусто: {f['label']}")
            continue
        if empty:
            continue
        if len(v) > f.get("max", 10_000):
            res.errors.append(f"{f['label']}: длиннее {f['max']} знаков")
        if f.get("pattern") and not re.match(f["pattern"], v):
            res.errors.append(f"{f['label']}: неверный формат ({f.get('hint', '')})")
        if f.get("min_items") and len([x for x in re.split(r"[;\n]", v) if x.strip()]) < f["min_items"]:
            res.warnings.append(f"{f['label']}: нужно {f['min_items']} пункта через «;»")
    res.fields = values
    res.flags = scan(text)
    res.ok = not res.errors
    res.sha256 = hashlib.sha256(text.encode()).hexdigest()
    return res
