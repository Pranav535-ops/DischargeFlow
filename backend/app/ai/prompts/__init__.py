"""Versioned prompt templates.

Each prompt is a file named `<name>@<version>.md` with `<<system>>` and `<<user>>` sections; `$placeholders` are
filled with string.Template. A published version is never edited: change a prompt by adding the next version and
pointing ACTIVE at it, so every stored generation keeps naming the exact text that produced it.
"""
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent
ACTIVE = {"discharge_summary": "v1", "assistant": "v1", "insights": "v1"}


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user: str
    sha256: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    def render_system(self, **values: str) -> str:
        return Template(self.system).substitute(values)

    def render_user(self, **values: str) -> str:
        return Template(self.user).substitute(values)


@lru_cache
def get_prompt(name: str, version: str | None = None) -> PromptTemplate:
    version = version or ACTIVE[name]
    # Line endings are normalised so the hash is the same on a Windows checkout and in CI.
    raw = (PROMPTS_DIR / f"{name}@{version}.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    _, _, rest = raw.partition("<<system>>")
    system, _, user = rest.partition("<<user>>")
    return PromptTemplate(name=name, version=version, system=system.strip(), user=user.strip(),
                          sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest())


def available_versions(name: str) -> list[str]:
    return sorted(p.stem.split("@", 1)[1] for p in PROMPTS_DIR.glob(f"{name}@*.md"))
