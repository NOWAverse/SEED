"""NOWA Registrar · interview — the bot asks questions one by one for people without their own AI.

Deterministic state machine: questions come from the questionnaire, answers are checked by the same
code as a pasted block. The language model (if any) may only rephrase a question or explain a field;
it never fills answers, never decides and never sees other people's data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .check import FLAG_TEXT, Questionnaire, check, render, scan

SKIP = {"пропустить", "skip", "-", "не указано", "нет"}


@dataclass
class Interview:
    q: Questionnaire
    answers: dict[str, str] = field(default_factory=dict)
    pos: int = 0

    def current(self) -> dict | None:
        return self.q.fields[self.pos] if self.pos < len(self.q.fields) else None

    def prompt(self) -> str:
        f = self.current()
        if f is None:
            return "Все вопросы пройдены."
        opt = "" if f.get("required") else " (можно «пропустить»)"
        return f"{self.pos + 1}/{len(self.q.fields)} · {f['ask']}{opt}\nПодсказка: {f['hint']}"

    def answer(self, text: str) -> str:
        """Returns a message for the person. Advances only on an acceptable answer."""
        f = self.current()
        if f is None:
            return "Анкета уже заполнена."
        t = text.strip()
        if t.lower() in SKIP:
            if f.get("required"):
                return "Это поле обязательное. " + f["hint"]
            self.answers[f["label"]] = "не указано"
            self.pos += 1
            return self.prompt()
        if len(t) > f.get("max", 10_000):
            return f"Слишком длинно (больше {f['max']} знаков). Сократи, пожалуйста."
        hits = scan(t)
        if hits:
            why = "; ".join(FLAG_TEXT[k] for k in hits)
            return f"Вижу в ответе: {why}. Это не нужно в NOWA — перефразируй без этого, пожалуйста."
        self.answers[f["label"]] = t.replace("\n", " ")
        probe = check(render(self.q, self.answers))
        bad = [e for e in probe.errors if e.startswith(f["label"])]
        if bad:
            self.answers.pop(f["label"], None)
            return bad[0]
        self.pos += 1
        return self.prompt()

    def done(self) -> bool:
        return self.current() is None

    def block(self) -> str:
        return render(self.q, self.answers)
