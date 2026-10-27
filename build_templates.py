#!/usr/bin/env python3
"""
Compile templates/*.md into templates.json for the OpenWhispr seeder.

Each Markdown file becomes one OpenWhispr note template (a row in the app's
`actions` table, kind = 'template'). File format:

    ---
    id: department-meeting
    name: Department Meeting
    description: Minutes for a department or staff meeting
    icon: clipboard-check
    ---
    Optional free-text instructions. These go into the template's prompt and
    the app treats them as "TEMPLATE INSTRUCTIONS" (max 20,000 chars).

    ## Attendees
    What the AI should write under this heading (max 2,000 chars).

    ## Decisions
    ...

id    stable slug; NEVER change it once deployed (it keys updates). Defaults
      to the file name.
name  shown in the template picker (max 200). Required.
description  optional (max 1000). icon  optional lucide icon name.
No inline comments in frontmatter values.

Every "## " heading becomes a section (max 20 sections, heading max 200).
A file with no "## " headings is a prompt-only template: the whole body is the
prompt. Use "###" or deeper inside an instruction if you need sub-headings.

A file WITHOUT frontmatter (or with "as: prompt" in it) is a prompt-only
template: the whole file, headings included, becomes the instructions, and
the name comes from the file name ("OpenWhispr IEP Meeting Summary
Template.md" -> "IEP Meeting Summary"). Its id is the slugified file name,
so renaming the file makes it a NEW template on Macs; add frontmatter with a
fixed id before renaming. README.md is ignored.

Usage:  python3 build_templates.py [templates_dir] [output.json]
"""
import json
import re
import sys
import uuid
from pathlib import Path

# Mirrors NOTE_ACTION_LIMITS in OpenWhispr 1.10.4 (src/helpers/builtinActions.js).
LIMITS = dict(name=200, description=1000, prompt=20000, sections=20, heading=200, instruction=2000)

# Fixed namespace so a template's id always maps to the same client_id UUID.
NAMESPACE = uuid.UUID("6b3f1c2e-9a41-4f7e-8d0b-2c5e7a9f4d10")

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
H2 = re.compile(r"^##\s+(?!#)(.+?)\s*#*\s*$")


def parse_frontmatter(block):
    meta = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"bad frontmatter line: {line!r}")
        meta[key.strip()] = value.strip().strip("'\"")
    return meta


def parse_body(body):
    prompt_lines, sections, current = [], [], None
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else H2.match(line)
        if m:
            current = {"heading": m.group(1).strip(), "lines": []}
            sections.append(current)
        elif current is None:
            prompt_lines.append(line)
        else:
            current["lines"].append(line)
    prompt = "\n".join(prompt_lines).strip()
    sections = [
        {"heading": s["heading"], "instruction": "\n".join(s["lines"]).strip()} for s in sections
    ]
    return prompt, sections


def slugify(stem):
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def name_from_filename(stem):
    # "OpenWhispr IEP Meeting Summary Template" -> "IEP Meeting Summary"
    name = re.sub(r"^\s*openwhispr\s+", "", stem, flags=re.I)
    name = re.sub(r"\s+template\s*$", "", name, flags=re.I)
    return name.strip() or stem


def compile_file(path):
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = FRONTMATTER.match(text)
    if m:
        meta, body = parse_frontmatter(m.group(1)), m.group(2)
    else:
        # No frontmatter: the whole file is one prompt, named after the file.
        meta, body = {"as": "prompt"}, text
    slug = meta.get("id") or slugify(path.stem)
    name = meta.get("name", "").strip() or name_from_filename(path.stem)
    description = meta.get("description", "").strip()
    icon = meta.get("icon", "").strip() or "file-text"
    if meta.get("as", "").lower() == "prompt":
        # Prompt-only: OpenWhispr appends the text to its own meeting-notes
        # system prompt, so headings inside it stay part of the prompt.
        prompt, sections = body.strip(), []
    else:
        prompt, sections = parse_body(body)

    errors = []
    if not prompt and not sections:
        errors.append("a template needs instructions or at least one ## section")
    if len(name) > LIMITS["name"]:
        errors.append(f"name longer than {LIMITS['name']}")
    if len(description) > LIMITS["description"]:
        errors.append(f"description longer than {LIMITS['description']}")
    if len(prompt) > LIMITS["prompt"]:
        errors.append(f"instructions longer than {LIMITS['prompt']}")
    if len(sections) > LIMITS["sections"]:
        errors.append(f"more than {LIMITS['sections']} sections")
    for s in sections:
        if len(s["heading"]) > LIMITS["heading"]:
            errors.append(f"heading too long: {s['heading'][:40]}…")
        if len(s["instruction"]) > LIMITS["instruction"]:
            errors.append(f"instruction under '{s['heading']}' longer than {LIMITS['instruction']}")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        "slug": slug,
        "client_id": str(uuid.uuid5(NAMESPACE, slug)),
        "name": name,
        "description": description,
        "icon": icon,
        "prompt": prompt,
        # Stored as a JSON string, exactly as the app stores it. "" = no sections.
        "sections": json.dumps(sections, ensure_ascii=False, separators=(",", ":")) if sections else "",
    }


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "templates")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "templates.json")
    files = sorted(f for f in src.glob("*.md") if f.name.lower() != "readme.md")
    if not files:
        sys.exit(f"no .md files in {src}")
    templates, failed, seen = [], False, {}
    for path in files:
        try:
            t = compile_file(path)
        except ValueError as e:
            print(f"✗ {path.name}: {e}", file=sys.stderr)
            failed = True
            continue
        if t["slug"] in seen:
            print(f"✗ {path.name}: id '{t['slug']}' already used by {seen[t['slug']]}", file=sys.stderr)
            failed = True
            continue
        seen[t["slug"]] = path.name
        templates.append(t)
        n = len(json.loads(t["sections"])) if t["sections"] else 0
        print(f"✓ {path.name} → {t['name']} ({n} sections)")
    if failed:
        sys.exit(1)
    out.write_text(
        json.dumps({"format": 1, "templates": templates}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {out} ({len(templates)} templates)")


if __name__ == "__main__":
    main()
