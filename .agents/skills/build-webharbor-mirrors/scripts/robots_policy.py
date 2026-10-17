"""Small RFC 9309-style robots.txt matcher used by intake and capture."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Rule:
    pattern: str
    allow: bool

    @property
    def match_length(self) -> int:
        value = self.pattern[:-1] if self.pattern.endswith("$") else self.pattern
        return len(value.replace("*", "").encode("utf-8"))

    def matches(self, path_query: str) -> bool:
        if not self.pattern:
            return False
        exact = self.pattern.endswith("$")
        value = self.pattern[:-1] if exact else self.pattern
        expression = "^" + ".*".join(re.escape(part) for part in value.split("*"))
        if exact:
            expression += "$"
        return re.search(expression, path_query) is not None


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay_value: float | None = None


class RobotsPolicy:
    """Parse robots rules without losing literal query or wildcard semantics."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url
        self.groups: list[Group] = []

    def parse(self, lines: list[str]) -> None:
        self.groups = []
        current = Group()
        saw_directive = False

        def finish() -> None:
            nonlocal current, saw_directive
            if current.agents:
                self.groups.append(current)
            current = Group()
            saw_directive = False

        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, raw_value = line.split(":", 1)
            name = field_name.strip().lower()
            value = raw_value.strip()
            if name == "user-agent":
                if current.agents and saw_directive:
                    finish()
                current.agents.append(value.lower())
                continue
            if not current.agents:
                continue
            if name in {"allow", "disallow"}:
                current.rules.append(Rule(value, name == "allow"))
                saw_directive = True
            elif name == "crawl-delay":
                try:
                    current.crawl_delay_value = float(value)
                except ValueError:
                    pass
                saw_directive = True
        finish()

    @staticmethod
    def _product_token(user_agent: str) -> str:
        return user_agent.strip().split(None, 1)[0].split("/", 1)[0].lower()

    def _matching_groups(self, user_agent: str) -> list[Group]:
        product = self._product_token(user_agent)
        matches: list[tuple[int, Group]] = []
        for group in self.groups:
            lengths = [
                len(agent)
                for agent in group.agents
                if agent != "*" and agent and agent in product
            ]
            if lengths:
                matches.append((max(lengths), group))
        if matches:
            longest = max(length for length, _ in matches)
            return [group for length, group in matches if length == longest]
        return [group for group in self.groups if "*" in group.agents]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parsed = urlsplit(url)
        path_query = parsed.path or "/"
        if parsed.query:
            path_query += "?" + parsed.query
        matching = [
            rule
            for group in self._matching_groups(user_agent)
            for rule in group.rules
            if rule.matches(path_query)
        ]
        if not matching:
            return True
        longest = max(rule.match_length for rule in matching)
        strongest = [rule for rule in matching if rule.match_length == longest]
        return any(rule.allow for rule in strongest)

    def crawl_delay(self, user_agent: str) -> float | None:
        for group in self._matching_groups(user_agent):
            if group.crawl_delay_value is not None:
                return group.crawl_delay_value
        return None
