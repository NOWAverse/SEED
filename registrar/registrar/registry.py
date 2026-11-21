"""NOWA Registrar · registry — source of truth about citizens and Planets (SQLite, standard library).

Rules enforced here, by code:
- an application becomes a citizen only after TWO different reviewers approve it;
- a reviewer cannot review their own submission or a submission of their own Planet;
- a Planet can be registered only by an existing citizen;
- every change is an append-only event; /withdraw erases personal data but keeps the fact of withdrawal.
The database lives on the NOWA cloud server, separate from members' servers; this code is public, the data is not.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
from pathlib import Path

from .check import Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS citizen   (id TEXT PRIMARY KEY, tg TEXT UNIQUE NOT NULL, nick TEXT, status TEXT NOT NULL,
                                      constellation TEXT, joined_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS planet    (id TEXT PRIMARY KEY, name TEXT, level INTEGER NOT NULL, constellation TEXT,
                                      status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ruler     (planet_id TEXT NOT NULL, citizen_id TEXT NOT NULL, PRIMARY KEY (planet_id, citizen_id));
CREATE TABLE IF NOT EXISTS submission(id TEXT PRIMARY KEY, tg TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
                                      body TEXT, sha256 TEXT NOT NULL, flags TEXT NOT NULL, consent TEXT,
                                      status TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision  (submission_id TEXT NOT NULL, reviewer TEXT NOT NULL, verdict TEXT NOT NULL,
                                      reason TEXT, at TEXT NOT NULL, PRIMARY KEY (submission_id, reviewer));
CREATE TABLE IF NOT EXISTS event     (n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
                                      kind TEXT NOT NULL, subject TEXT, info TEXT);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
"""

REVIEWS_NEEDED = 2
PHASE1_LIMIT = 300


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- log -------------------------------------------------------------
    def log(self, actor: str, kind: str, subject: str | None = None, **info) -> None:
        self.db.execute("INSERT INTO event(at, actor, kind, subject, info) VALUES (?,?,?,?,?)",
                        (now(), actor, kind, subject, json.dumps(info, ensure_ascii=False) if info else None))

    # ---- intake ----------------------------------------------------------
    def submit(self, tg: str, res: Result, consent: str) -> str:
        """Store a checked block. `consent` is the person's own words, asked by the bot (never written by an AI)."""
        if not res.ok:
            raise RegistryError("блок не прошёл проверку: " + "; ".join(res.errors))
        if not consent or len(consent.strip()) < 10:
            raise RegistryError("нужно согласие своими словами")
        if res.kind == "citizen":
            if self.citizen_by_tg(tg):
                raise RegistryError("ты уже гражданин NOWA")
            if self.count_citizens() >= PHASE1_LIMIT:
                raise RegistryError(f"лимит первой фазы ({PHASE1_LIMIT}) достигнут — заявка в листе ожидания")
        if res.kind == "planet":
            c = self.citizen_by_tg(tg)
            if not c:
                raise RegistryError("сначала гражданство, потом Планета")
            if res.fields.get("ID гражданина") != c["id"]:
                raise RegistryError("ID гражданина в блоке не совпадает с твоим")
        sid = "A-" + dt.date.today().strftime("%y%m%d") + "-" + secrets.token_hex(4).upper()
        with self.db:
            self.db.execute("INSERT INTO submission VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sid, tg, res.kind, res.version, res.text, res.sha256, json.dumps(sorted(res.flags)),
                             consent.strip(), "pending", now()))
            self.log(f"tg:{tg}", "submitted", sid, form=res.kind, flags=sorted(res.flags))
        return sid

    # ---- review ----------------------------------------------------------
    def review(self, sid: str, reviewer: str, verdict: str, reason: str = "") -> str:
        """reviewer = citizen id of an admin. Returns new status of the submission."""
        if verdict not in ("approve", "clarify", "reject"):
            raise RegistryError("verdict: approve | clarify | reject")
        s = self.db.execute("SELECT * FROM submission WHERE id=?", (sid,)).fetchone()
        if not s or s["status"] != "pending":
            raise RegistryError("заявка не найдена или уже решена")
        rev = self.db.execute("SELECT * FROM citizen WHERE id=?", (reviewer,)).fetchone()
        if not rev or rev["status"] != "active":
            raise RegistryError("рецензент должен быть действующим гражданином")
        if rev["tg"] == s["tg"]:
            raise RegistryError("нельзя рецензировать свою заявку")
        if s["kind"] == "planet":
            own = self.citizen_by_tg(s["tg"])
            mine = self.db.execute("SELECT 1 FROM ruler r1 JOIN ruler r2 ON r1.planet_id=r2.planet_id "
                                   "WHERE r1.citizen_id=? AND r2.citizen_id=?", (reviewer, own["id"])).fetchone()
            if mine:
                raise RegistryError("нельзя рецензировать Планету, где ты Правитель")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO decision VALUES (?,?,?,?,?)", (sid, reviewer, verdict, reason, now()))
            self.log(reviewer, "reviewed", sid, verdict=verdict)
            if verdict in ("reject", "clarify"):
                self.db.execute("UPDATE submission SET status=? WHERE id=?", (verdict, sid))
                return verdict
            n = self.db.execute("SELECT COUNT(*) FROM decision WHERE submission_id=? AND verdict='approve'", (sid,)).fetchone()[0]
            if n < REVIEWS_NEEDED:
                return "pending"
            self.db.execute("UPDATE submission SET status='approved' WHERE id=?", (sid,))
            return self._accept(s)

    def _accept(self, s: sqlite3.Row) -> str:
        if s["kind"] == "citizen":
            cid = self._next_id("C", "citizen")
            nick = _field(s["body"], "Имя")
            self.db.execute("INSERT INTO citizen VALUES (?,?,?,?,?,?)", (cid, s["tg"], nick, "active", None, now()))
            self.log("registrar", "citizen_created", cid, submission=s["id"])
            return cid
        pid = self._next_id("P", "planet")
        c = self.citizen_by_tg(s["tg"])
        self.db.execute("INSERT INTO planet VALUES (?,?,?,?,?,?)", (pid, _field(s["body"], "Название Планеты"), 1, None, "active", now()))
        self.db.execute("INSERT INTO ruler VALUES (?,?)", (pid, c["id"]))
        self.log("registrar", "planet_created", pid, ruler=c["id"], submission=s["id"])
        return pid

    # ---- withdraw --------------------------------------------------------
    def withdraw(self, tg: str) -> None:
        """Erase personal data of this person; keep the fact (event) of withdrawal."""
        c = self.citizen_by_tg(tg)
        with self.db:
            self.db.execute("UPDATE submission SET body=NULL, consent=NULL, status='withdrawn' WHERE tg=?", (tg,))
            if c:
                self.db.execute("UPDATE citizen SET nick=NULL, status='withdrawn', tg=? WHERE id=?", (f"withdrawn:{c['id']}", c["id"]))
                self.db.execute("UPDATE planet SET status='withdrawn', name=NULL WHERE id IN "
                                "(SELECT planet_id FROM ruler WHERE citizen_id=?) AND id NOT IN "
                                "(SELECT planet_id FROM ruler r JOIN citizen ci ON ci.id=r.citizen_id WHERE ci.status='active' AND ci.id<>?)",
                                (c["id"], c["id"]))
            self.log(c["id"] if c else f"tg:{tg}", "withdrawn", c["id"] if c else None)

    # ---- helpers ---------------------------------------------------------
    def citizen_by_tg(self, tg: str):
        return self.db.execute("SELECT * FROM citizen WHERE tg=?", (tg,)).fetchone()

    def count_citizens(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM citizen WHERE status='active'").fetchone()[0]

    def status(self, tg: str) -> list[dict]:
        rows = self.db.execute("SELECT id, kind, status, received_at FROM submission WHERE tg=? ORDER BY received_at", (tg,))
        return [dict(r) for r in rows]

    def bootstrap_admin(self, tg: str, nick: str) -> str:
        """First operator(s) before any reviewers exist. Logged as such; only for an empty registry."""
        if self.db.execute("SELECT COUNT(*) FROM citizen").fetchone()[0] >= 2:
            raise RegistryError("bootstrap only while the registry has fewer than 2 citizens")
        cid = self._next_id("C", "citizen")
        with self.db:
            self.db.execute("INSERT INTO citizen VALUES (?,?,?,?,?,?)", (cid, tg, nick, "active", None, now()))
            self.log("operator", "bootstrap_admin", cid)
        return cid

    def _next_id(self, prefix: str, table: str) -> str:
        n = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1
        return f"{prefix}{n:03d}"


def _field(body: str, label: str) -> str | None:
    for line in (body or "").splitlines():
        if line.startswith(f"- {label}:"):
            v = line.split(":", 1)[1].strip()
            return None if v in ("", "не указано") else v
    return None
