#!/usr/bin/env python3
"""Find skills in the current project.

Reads .claude-plugin/plugin.json for custom skill paths, falls back to
default locations (.claude/skills, skills). Excludes eval harness skills.

Usage:
    python3 ${CLAUDE_SKILL_DIR}/scripts/find_skills.py [--name <skill>]
"""

import agent_eval._bootstrap  # noqa: F401 — auto-activate venv

import argparse
import json
import sys
from glob import glob
from pathlib import Path

import yaml

# Default directories where skills live in a project
DEFAULT_SKILL_DIRS = [".claude/skills", "skills"]

# Skills from the eval harness — excluded from discovery
HARNESS_SKILLS = {"eval-setup", "eval-analyze", "eval-dataset", "eval-run",
                   "eval-review", "eval-mlflow", "eval-optimize"}


def get_skill_dirs():
    """Get skill directories for the current project.

    Reads .claude-plugin/plugin.json for a custom 'skills' field.
    Falls back to the default locations.
    """
    plugin_json = Path(".claude-plugin/plugin.json")
    if plugin_json.exists():
        try:
            with open(plugin_json) as f:
                manifest = json.load(f)
            skills_field = manifest.get("skills")
            if skills_field:
                if isinstance(skills_field, str):
                    return [skills_field.lstrip("./")]
                elif isinstance(skills_field, list):
                    return [s.lstrip("./") for s in skills_field]
        except Exception:
            pass
    return DEFAULT_SKILL_DIRS


def find_skill(name):
    """Find a skill's SKILL.md by name.

    Returns the Path to SKILL.md or None if not found.
    """
    for skills_dir in get_skill_dirs():
        skill_path = Path(skills_dir) / name / "SKILL.md"
        if skill_path.exists():
            return skill_path
    return None


def list_skills():
    """List all project skills (excluding harness skills).

    Returns list of dicts: [{name, path, description}, ...]
    """
    skills = []
    for skills_dir in get_skill_dirs():
        for path in sorted(glob(f"{skills_dir}/*/SKILL.md")):
            name = Path(path).parent.name
            if name in HARNESS_SKILLS:
                continue
            desc = ""
            try:
                with open(path) as f:
                    content = f.read()
                if content.startswith("---"):
                    fm = yaml.safe_load(content.split("---")[1])
                    desc = (fm or {}).get("description", "")[:80]
            except Exception as e:
                print(f"  WARNING: failed to parse {path}: {e}", file=sys.stderr)
            skills.append({"name": name, "path": path, "description": desc})
    return skills


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default=None,
                        help="Find a specific skill by name")
    args = parser.parse_args()

    if args.name:
        path = find_skill(args.name)
        if path:
            print(f"FOUND: {path}")
        else:
            print(f"NOT_FOUND: {args.name}")
            sys.exit(1)
    else:
        skills = list_skills()
        if skills:
            for s in skills:
                print(f"SKILL: {s['name']:<30} {s['description']}")
        else:
            print("NONE: no skills found")


if __name__ == "__main__":
    main()
