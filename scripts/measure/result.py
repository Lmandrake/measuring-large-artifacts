"""The typed Measurement — option D of SCANNED_ARTIFACTS_CANNOT_LIE_1.

Every question about a large artifact returns one of exactly three things, and
they are not interchangeable:

    Measured(value, ...)    the number is real, and it says what it was measured against
    Unmeasured(reason)      the artifact does not carry the evidence — NOT zero
    Refused(reason)         the instrument cannot judge this question — NOT zero

The single highest-value property of this module is that `0` can only ever mean
"measured zero". Ignorance has its own type, and it renders as its own word.

Each renders to ONE line, because context is the budget: a count question must
cost fewer than 100 tokens of output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Measurement:
    """Base. Never returned directly."""

    #: True only for Measured. Use this instead of truth-testing the value —
    #: `if m.value:` is exactly the bug this module exists to prevent, because
    #: Unmeasured has no value at all and Measured(0) is falsy.
    ok = False

    def line(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def __str__(self) -> str:
        return self.line()

    def unwrap(self) -> Any:
        """The value, or raise. Use when a wrong answer must stop the program."""
        raise UnmeasuredError(self.line())


class UnmeasuredError(RuntimeError):
    """Raised by .unwrap() on anything that is not a Measured."""


@dataclass
class Measured(Measurement):
    """A real answer, carrying what it was measured against."""

    value: Any
    instrument: str
    artifact: str
    #: What makes this reproducible — a dump id, a commit sha, a mod-set
    #: fingerprint. Goal 4: same artifact + same question => same answer, and
    #: the answer says which artifact.
    against: str = ""
    #: Optional short evidence, e.g. "3 of 12 rows". Never a record dump.
    evidence: str = ""

    ok = True

    def unwrap(self) -> Any:
        return self.value

    def line(self) -> str:
        parts = [f"MEASURED {self.value} {self.artifact}"]
        if self.evidence:
            parts.append(f"({self.evidence})")
        parts.append(f"via {self.instrument}")
        if self.against:
            parts.append(f"@ {self.against}")
        return " ".join(parts)


@dataclass
class Unmeasured(Measurement):
    """The artifact does not carry the evidence. This is not zero.

    Cause B in the analysis: the dump lost or flattened what was asked about.
    `remedy` is mandatory in spirit — an Unmeasured that does not say how to
    get the number is a dead end, and the next agent will reach for grep.
    """

    reason: str
    artifact: str
    instrument: str = ""
    remedy: str = ""

    def line(self) -> str:
        parts = [f"UNMEASURED {self.artifact} — {self.reason}"]
        if self.instrument:
            parts.append(f"[{self.instrument}]")
        if self.remedy:
            parts.append(f"remedy: {self.remedy}")
        return " ".join(parts)


@dataclass
class Refused(Measurement):
    """This instrument cannot judge this question. This is not zero.

    Cause C in the analysis: the checker's semantics are narrower than reality.
    `right_instrument` is the whole point — a refusal that does not name the
    correct path is an obstacle, and obstacles get routed around.
    """

    reason: str
    artifact: str
    right_instrument: str = ""
    instrument: str = ""

    def line(self) -> str:
        parts = [f"REFUSED {self.artifact} — {self.reason}"]
        if self.instrument:
            parts.append(f"[{self.instrument}]")
        if self.right_instrument:
            parts.append(f"use instead: {self.right_instrument}")
        return " ".join(parts)


@dataclass
class Report:
    """Several measurements answered together, still one line each.

    Used by the selftest and by any audit that asks the same question of many
    subjects. It deliberately has no aggregate `total`: summing across a set
    that contains an Unmeasured is how a partial capture becomes a confident
    wrong number.
    """

    rows: list = field(default_factory=list)

    def add(self, m: Measurement) -> None:
        self.rows.append(m)

    @property
    def measured(self) -> list:
        return [r for r in self.rows if r.ok]

    @property
    def unmeasured(self) -> list:
        return [r for r in self.rows if not r.ok]

    def total(self) -> Measurement:
        """Sum, but only if every row is Measured. Otherwise Unmeasured."""
        bad = self.unmeasured
        if bad:
            return Unmeasured(
                reason=f"{len(bad)} of {len(self.rows)} subjects are not measured, "
                f"so no total exists (first: {bad[0].artifact})",
                artifact="total",
                remedy="fix or exclude the unmeasured subjects, then ask again",
            )
        return Measured(
            value=sum(r.value for r in self.rows),
            instrument="Report.total",
            artifact="total",
            evidence=f"{len(self.rows)} subjects",
        )

    def text(self) -> str:
        return "\n".join(r.line() for r in self.rows)
