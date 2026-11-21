"""CLI:  python3 -m registrar check <file.md>        validate a block (prints result, never the field values)
      python3 -m registrar template citizen|planet  print the empty block for an AI assistant
      python3 -m registrar interview citizen|planet  ask the questions one by one in the terminal (demo of the bot)
      python3 -m registrar seed-templates <dir>      regenerate SEED templates/ from the questionnaires
"""
import json
import sys
from pathlib import Path

from .check import check, load, render
from .interview import Interview


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "check":
        r = check(Path(argv[1]).read_text(encoding="utf-8"))
        print(json.dumps(r.public(), ensure_ascii=False, indent=1))
        return 0 if r.ok else 1
    if len(argv) == 2 and argv[0] == "template":
        print(render(load(argv[1]), {}).replace("не указано", ""))
        return 0
    if len(argv) == 2 and argv[0] == "interview":
        iv = Interview(load(argv[1]))
        print(iv.prompt())
        while not iv.done():
            print(iv.answer(input("> ")))
        print("\n" + iv.block())
        print(json.dumps(check(iv.block()).public(), ensure_ascii=False, indent=1))
        return 0
    if len(argv) == 2 and argv[0] == "seed-templates":
        out = Path(argv[1])
        for kind in ("citizen", "planet"):
            q = load(kind)
            hints = "\n".join(f"- **{f['label']}**{' *(обязательно)*' if f.get('required') else ''} — {f['hint']}" for f in q.fields)
            body = render(q, {}).replace("не указано", "")
            (out / f"{kind}_L{q.level}.md").write_text(
                f"<!-- generated from REGISTRAR/questionnaires/{kind}_L{q.level}.toml v{q.version} -->\n"
                f"{body}\n\nПодсказки:\n{hints}\n", encoding="utf-8")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
