"""Keeps the Claude Code instructions and docs honest: skill/agent file formats, that every command and flag the docs tell
people to run still exists, settings.json sanity, and a Mermaid syntax lint (the diagrams cannot be rendered in tests, so the
lint catches the mistakes that make GitHub show a diagram error: unquoted parentheses, stray semicolons, unbalanced subgraphs).

Run: python3 -m unittest scripts/test_docs.py -v
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").glob("*.md")),
             *sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md")), *sorted((ROOT / ".claude" / "agents").glob("*.md"))]

SKILL_FIELDS = {"name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation", "user-invocable",
                "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent", "background", "hooks", "paths",
                "shell", "metadata", "license", "compatibility"}
AGENT_FIELDS = {"name", "description", "tools", "disallowedTools", "model", "permissionMode", "maxTurns", "skills", "mcpServers",
                "hooks", "memory", "background", "effort", "isolation", "color", "initialPrompt", "omitClaudeMd"}
# skills that can write to Builder or change manifest state must never be started by the model on its own
USER_ONLY_SKILLS = {"run-migration", "triage-needs-human"}


def frontmatter(path):
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fm = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip() and not line.startswith(" "):
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def mermaid_blocks(text):
    return re.findall(r"```mermaid\n(.*?)```", text, re.S)


def mermaid_problems(block):
    """Heuristic lint for the mistakes that break Mermaid rendering. Returns a list of problems (empty = looks fine)."""
    problems = []
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if not lines:
        return ["empty diagram"]
    kind = lines[0].strip().split()[0]
    if kind not in {"flowchart", "graph", "sequenceDiagram", "stateDiagram-v2", "stateDiagram", "classDiagram", "erDiagram", "gantt", "pie"}:
        problems.append(f"unknown diagram type {kind!r}")
    for i, ln in enumerate(lines, 1):
        if ln.count('"') % 2:
            problems.append(f"line {i}: unbalanced double quotes: {ln.strip()}")
        if kind in ("flowchart", "graph"):
            if re.search(r"\b[A-Za-z_]\w*\[[^\]\"]*[(){}][^\]\"]*\]", ln):
                problems.append(f"line {i}: parentheses/braces in an unquoted [label]: {ln.strip()}")
            if re.search(r"\b[A-Za-z_]\w*\{[^}\"]*[()][^}\"]*\}", ln):
                problems.append(f"line {i}: parentheses in an unquoted {{decision}}: {ln.strip()}")
        if kind == "sequenceDiagram" and re.search(r"-{1,2}>>?|-{1,2}x|-{1,2}\)", ln) and ":" in ln:
            body = ln.split(":", 1)[1]
            if ";" in body or "#" in body:
                problems.append(f"line {i}: ';' or '#' in a message (Mermaid treats them as separators/comments): {ln.strip()}")
        if kind.startswith("stateDiagram") and "-->" in ln and ln.count(":") > 1:
            problems.append(f"line {i}: more than one ':' in a transition label: {ln.strip()}")
    if kind in ("flowchart", "graph"):
        opened = sum(1 for ln in lines if ln.strip().startswith("subgraph"))
        closed = sum(1 for ln in lines if ln.strip() == "end")
        if opened != closed:
            problems.append(f"{opened} subgraph(s) but {closed} 'end'")
    return problems


class Skills(unittest.TestCase):
    def skills(self):
        return sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))

    def test_there_are_skills(self):
        self.assertGreaterEqual(len(self.skills()), 4)

    def test_every_skill_has_valid_frontmatter(self):
        for path in self.skills():
            with self.subTest(skill=path.parent.name):
                fm, body = frontmatter(path)
                self.assertIsNotNone(fm, "missing --- frontmatter ---")
                self.assertEqual(fm.get("name"), path.parent.name, "name must match the directory")
                self.assertTrue(fm.get("description"), "description is required for Claude to know when to use it")
                self.assertLessEqual(len(fm["description"]), 1024)
                self.assertEqual(set(fm) - SKILL_FIELDS, set(), "unknown frontmatter field")
                self.assertLess(len(path.read_text().splitlines()), 500, "keep SKILL.md under 500 lines")
                self.assertTrue(body.strip().startswith("#"), "the body should start with a heading")

    def test_skills_that_write_are_user_started_only(self):
        found = {p.parent.name: frontmatter(p)[0] for p in self.skills()}
        for name in USER_ONLY_SKILLS:
            with self.subTest(skill=name):
                self.assertIn(name, found)
                self.assertEqual(found[name].get("disable-model-invocation"), "true", "a skill that writes must not auto-trigger")

    def test_skills_do_not_pre_grant_permissions(self):
        for path in self.skills():
            self.assertNotIn("allowed-tools", frontmatter(path)[0], f"{path.parent.name}: permissions belong in settings, not in a skill")

    def test_skills_never_instruct_approving_the_mapping_themselves(self):
        for path in self.skills():
            text = path.read_text()
            for line in text.splitlines():
                if "mapping.py approve" in line:
                    self.assertTrue(any(w in line.lower() for w in ("never", "approve it yourself", "!", "themselves", "human", "user")),
                                    f"{path.parent.name}: {line.strip()}")

    def test_the_skills_listed_in_the_readme_exist(self):
        readme = (ROOT / "README.md").read_text()
        listed = set(re.findall(r"^\| `/([a-z-]+)", readme, re.M))
        self.assertEqual(listed, {p.parent.name for p in self.skills()})


class Agents(unittest.TestCase):
    def test_agent_files_are_well_formed(self):
        files = sorted((ROOT / ".claude" / "agents").glob("*.md"))
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            with self.subTest(agent=path.stem):
                fm, body = frontmatter(path)
                self.assertEqual(fm.get("name"), path.stem)
                self.assertTrue(fm.get("description"))
                self.assertEqual(set(fm) - AGENT_FIELDS, set())
                self.assertIn(fm.get("model"), {"sonnet", "opus", "haiku", "fable", "inherit", None})

    def test_agents_never_get_write_tools_for_contentful_or_publish_tools_for_builder(self):
        for path in (ROOT / ".claude" / "agents").glob("*.md"):
            tools = frontmatter(path)[0].get("tools", "")
            for bad in ("create_entry", "update_entry", "publish", "delete", "create_symbol"):
                self.assertNotIn(bad, tools, f"{path.stem} must not have {bad}")


class Commands(unittest.TestCase):
    """Every `python3 scripts/X.py ...` in the docs must name a real script, a real subcommand and real flags."""

    @staticmethod
    def commands(text):
        for m in re.finditer(r"python3 (scripts/[\w/]+\.py)([^\n`|]*)", text):
            yield m.group(1), m.group(2)

    def test_documented_commands_exist(self):
        checked = 0
        for path in DOC_FILES:
            for script, rest in self.commands(path.read_text()):
                checked += 1
                src_path = ROOT / script
                self.assertTrue(src_path.exists(), f"{path.name}: {script} does not exist")
                src = src_path.read_text()
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", rest):
                    if flag == "--help":  # argparse provides it; it never appears in the source text
                        continue
                    self.assertTrue(flag in src, f"{path.name}: {script} has no {flag}")
                first = rest.split()[0] if rest.split() else ""
                if re.fullmatch(r"[a-z][a-z-]*", first) and not first.startswith("<"):
                    self.assertTrue(f'"{first}"' in src or f"'{first}'" in src, f"{path.name}: {script} has no subcommand {first!r}")
        self.assertGreater(checked, 40)

    def test_make_targets_in_docs_exist(self):
        makefile = (ROOT / "Makefile").read_text()
        targets = set(re.findall(r"^([a-z-]+):", makefile, re.M))
        for path in DOC_FILES:
            for target in re.findall(r"`make ([a-z-]+)`", path.read_text()):
                self.assertIn(target, targets, f"{path.name}: make {target}")

    def test_the_linter_for_this_test_catches_a_wrong_flag(self):
        src = (ROOT / "scripts" / "run_migration.py").read_text()
        self.assertIn("--apply", src)
        self.assertNotIn("--definitely-not-a-flag", src)


class Settings(unittest.TestCase):
    def setUp(self):
        self.s = json.loads((ROOT / ".claude" / "settings.json").read_text())

    def test_shape(self):
        self.assertEqual(set(self.s["permissions"]) - {"allow", "ask", "deny", "defaultMode"}, set())

    def test_rules_reference_scripts_that_exist(self):
        for rule in sum((self.s["permissions"][k] for k in ("allow", "ask", "deny")), []):
            for script in re.findall(r"python3 (scripts/[\w/]+\.py)", rule):
                self.assertTrue((ROOT / script).exists(), f"{rule}: {script} missing")

    def test_the_hook_runs_the_guard_on_every_mcp_call(self):
        hooks = self.s["hooks"]["PreToolUse"]
        self.assertTrue(any(h["matcher"] == "mcp__.*" and "scripts/guard.py" in h["hooks"][0]["command"] for h in hooks))
        self.assertTrue((ROOT / "scripts" / "guard.py").exists())

    def test_the_key_safety_rules_are_present(self):
        deny, ask = self.s["permissions"]["deny"], self.s["permissions"]["ask"]
        self.assertIn("Read(./.env)", deny)
        for tool in ("create_entry", "update_entry", "publish_entry", "delete_entry"):
            self.assertIn(f"mcp__contentful__{tool}", deny)
        self.assertIn("Bash(python3 scripts/manifest.py requeue:*)", ask)
        self.assertIn("Bash(python3 scripts/manifest.py skip:*)", ask)
        self.assertIn("Bash(python3 scripts/mapping.py approve)", ask)

    def test_ask_beats_allow_for_bulk_manifest_commands(self):
        """Claude Code evaluates deny, then ask, then allow; requeue/skip are covered by the broad manifest allow, so the ask rule must exist."""
        allow = self.s["permissions"]["allow"]
        self.assertIn("Bash(python3 scripts/manifest.py:*)", allow)
        self.assertIn("Bash(python3 scripts/manifest.py requeue:*)", self.s["permissions"]["ask"])

    def test_the_project_mcp_file_has_the_contentful_server(self):
        mcp = json.loads((ROOT / ".mcp.json").read_text())
        self.assertIn("contentful", mcp["mcpServers"])


class ClaudeMd(unittest.TestCase):
    def test_it_stays_small_enough_to_be_followed(self):
        self.assertLessEqual(len((ROOT / "CLAUDE.md").read_text().splitlines()), 200)  # the documented target

    def test_it_mentions_every_skill(self):
        text = (ROOT / "CLAUDE.md").read_text()
        for p in (ROOT / ".claude" / "skills").glob("*/SKILL.md"):
            self.assertIn(f"/{p.parent.name}", text)


class Mermaid(unittest.TestCase):
    def test_every_diagram_passes_the_lint(self):
        seen = 0
        for path in DOC_FILES:
            text = path.read_text()
            self.assertEqual(text.count("```") % 2, 0, f"{path.name}: unbalanced code fences")
            for i, block in enumerate(mermaid_blocks(text), 1):
                seen += 1
                self.assertEqual(mermaid_problems(block), [], f"{path.name} diagram {i}")
        self.assertGreaterEqual(seen, 6)

    def test_no_ascii_art_diagrams_are_left_in_the_readme(self):
        readme = (ROOT / "README.md").read_text()
        for block in re.findall(r"```\n(.*?)```", readme, re.S):
            self.assertFalse(re.search(r"[─│┌┐└┘├┤►▼]|──", block), "use a mermaid diagram instead of box-drawing characters")

    def test_the_lint_itself_catches_the_classic_mistakes(self):
        bad = {
            "unquoted parentheses": 'flowchart LR\n  A[Contentful (read-only)] --> B[x]',
            "unbalanced quotes": 'flowchart LR\n  A["x] --> B[y]',
            "unbalanced subgraph": 'flowchart TB\n  subgraph S["s"]\n    A["a"]\n  B["b"]',
            "semicolon in message": 'sequenceDiagram\n  A->>B: do this; then that',
            "hash in message": 'sequenceDiagram\n  A->>B: see #12',
            "two colons": 'stateDiagram-v2\n  a --> b: x: y',
            "unknown type": 'flowchar LR\n  A --> B',
        }
        for name, block in bad.items():
            with self.subTest(name=name):
                self.assertTrue(mermaid_problems(block), name)
        good = 'flowchart LR\n  A["Contentful (read-only)"] --> B{"ok?"}\n  subgraph S["s"]\n    C["c"]\n  end'
        self.assertEqual(mermaid_problems(good), [])


if __name__ == "__main__":
    unittest.main()
