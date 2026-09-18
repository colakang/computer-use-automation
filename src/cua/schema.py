"""The capability artifact: a typed, versioned, reviewable contract for one UI flow.

Design stance
-------------
An artifact is *not* a macro recording. It is a function signature plus a
body the machine can execute without a model:

    member.savings_balance.read(member_id: str) -> {savings_balance: money, member_name: str}
        | business_outcome(RECORD_NOT_FOUND | INVALID_INPUT)
        | failure(kind, step, expected, observed)

Four things are deliberately separated:

* **Contract** (``inputs``, ``outputs``, ``outcomes``) — what a calling agent
  needs. Exportable as a tool/function schema. Written by a human in the goal
  spec; the model never invents it.
* **Body** (``steps``) — how to do it on one surface. Discovered by the model,
  then frozen. Every target carries *several* locator strategies, ordered by
  robustness, each verified unique at record time.
* **Environment knowledge** (conditions such as "session expired",
  "system notice", "no matching records") — lives in the *app profile*, not
  in each capability, because it is a property of the vendor product and is
  shared by every capability and every tenant running that product.
* **Specialisation** — tenant / vendor-version differences are expressed as
  :class:`Overlay` patches against a base capability, never as copies.

Everything is Pydantic so the same model validates files, renders JSON
Schema for reviewers, and produces the tool schema for calling agents.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "cua.capability/v1"

_TEMPLATE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- contract


class Sensitivity(StrEnum):
    """Drives redaction. Anything above ``internal`` never reaches logs verbatim."""

    public = "public"
    internal = "internal"          # e.g. member number: fine in the call, masked in logs
    pii = "pii"                    # names, DOB, addresses
    confidential = "confidential"  # balances, account data
    secret = "secret"              # credentials — never leaves the secret store


class ParamType(StrEnum):
    string = "string"
    integer = "integer"
    money = "money"     # parsed to {"amount": "12403.22", "currency": "USD"}
    enum = "enum"
    boolean = "boolean"


class InputParam(Strict):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: ParamType
    description: str
    required: bool = True
    pattern: str | None = Field(None, description="Regex the value must fully match (validated before touching the UI).")
    enum: list[str] | None = None
    sensitivity: Sensitivity = Sensitivity.internal

    def validate_value(self, raw: Any) -> str:
        v = "" if raw is None else str(raw)
        if self.type == ParamType.integer and not re.fullmatch(r"-?\d+", v):
            raise ValueError(f"{self.name}: expected integer")
        if self.type == ParamType.money:
            try:
                Decimal(v.replace("$", "").replace(",", ""))
            except InvalidOperation:
                raise ValueError(f"{self.name}: expected money amount") from None
        if self.type == ParamType.enum and self.enum and v not in self.enum:
            raise ValueError(f"{self.name}: must be one of {self.enum}")
        if self.pattern and not re.fullmatch(self.pattern, v):
            raise ValueError(f"{self.name}: does not match {self.pattern}")
        return v


class OutputField(Strict):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: ParamType
    description: str
    sensitivity: Sensitivity = Sensitivity.confidential


def parse_output(field: OutputField, text: str) -> Any:
    t = text.strip()
    if field.type == ParamType.money:
        neg = t.startswith("(") or t.startswith("-")
        digits = re.sub(r"[^\d.]", "", t)
        amount = Decimal(digits)
        return {"amount": str(-amount if neg else amount), "currency": "USD"}
    if field.type == ParamType.integer:
        return int(re.sub(r"[^\d-]", "", t))
    if field.type == ParamType.boolean:
        return t.lower() in {"yes", "true", "y", "1", "active"}
    return t


# --------------------------------------------------------------------------- targeting


class RoleName(Strict):
    """Visible role + accessible name — what a screen reader (or a human) sees."""

    by: Literal["role_name"] = "role_name"
    role: str
    name: str


class VisualLabel(Strict):
    """Unlabeled field identified by the caption beside it ("Search Value: [___]")."""

    by: Literal["label"] = "label"
    role: str
    label: str


class RowMatch(Strict):
    column: str
    equals: str


class TableCell(Strict):
    """Cell addressed by meaning: column header × the row whose <column> equals <value>."""

    by: Literal["table_cell"] = "table_cell"
    column: str
    row: RowMatch


class Attribute(Strict):
    """A markup attribute such as a form field's ``name`` (F_0108). Invisible to users, stable-ish in vendor apps."""

    by: Literal["attr"] = "attr"
    tag: str
    attr: str
    value: str


class CssPath(Strict):
    """Structural path. Last resort: survives nothing but a byte-identical page."""

    by: Literal["css"] = "css"
    path: str


Strategy = Annotated[RoleName | VisualLabel | TableCell | Attribute | CssPath, Field(discriminator="by")]


class Target(Strict):
    """How to find one control. Strategies are tried in order; the first that
    matches *exactly one* element wins. Zero or many matches never fall
    through to a guess — ambiguity is a failure, not a coin toss.
    Strategy fields may contain ``{{input}}`` templates."""

    frame: str = Field(description="Frame name ('main', 'nav', '_top'); desktop surfaces would use a window path.")
    description: str
    strategies: list[Strategy] = Field(min_length=1)


# --------------------------------------------------------------------------- checkpoints


class UrlIs(Strict):
    kind: Literal["url"] = "url"
    frame: str
    path: str = Field(description="Path+query relative to the tenant base URL; may contain {{input}} templates.")


class ElementPresent(Strict):
    kind: Literal["element"] = "element"
    target: Target


class TextPresent(Strict):
    kind: Literal["text"] = "text"
    frame: str
    contains: str


Condition = Annotated[UrlIs | ElementPresent | TextPresent, Field(discriminator="kind")]


class Checkpoint(Strict):
    all_of: list[Condition] = Field(min_length=1)


# --------------------------------------------------------------------------- steps


class ValueRef(Strict):
    """Where a typed/selected value comes from. Never an inline secret."""

    input: str | None = None
    secret: str | None = None
    literal: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ValueRef:
        if sum(x is not None for x in (self.input, self.secret, self.literal)) != 1:
            raise ValueError("ValueRef needs exactly one of input/secret/literal")
        return self


class Risk(StrEnum):
    read = "read"                  # observation only
    reversible = "reversible"      # navigation, typing into a form, opening a review screen
    irreversible = "irreversible"  # commits something on the system of record


class Step(Strict):
    id: str
    intent: str = Field(description="One line a reviewer can read: why this step exists.")
    action: Literal["click", "fill", "select", "press", "extract"]
    target: Target
    value: ValueRef | None = None
    key: str | None = None
    output: str | None = Field(None, description="For extract: which declared output this fills.")
    risk: Risk = Risk.reversible
    idempotent: bool = Field(True, description="Safe to re-execute on a transient failure.")
    phase: Literal["auth", "main"] = "main"
    expect: Checkpoint | None = Field(None, description="Post-condition asserted after the action.")
    timeout_ms: int = 15_000
    provenance: Literal["model", "human"] = "model"

    @model_validator(mode="after")
    def _shape(self) -> Step:
        if self.action in ("fill", "select") and self.value is None:
            raise ValueError(f"{self.id}: {self.action} needs a value")
        if self.action == "extract" and not self.output:
            raise ValueError(f"{self.id}: extract needs an output")
        if self.risk == Risk.irreversible and self.idempotent:
            raise ValueError(f"{self.id}: irreversible steps cannot be idempotent")
        return self


class OutcomeMapping(Strict):
    """Maps an app-profile business condition to this capability's vocabulary."""

    condition: str
    code: str
    description: str


class AppRef(Strict):
    product: str
    vendor: str
    versions: list[str] = Field(description="Vendor versions this body was recorded/validated against.")
    surface: Literal["web", "legacy_web", "desktop"]


class Provenance(Strict):
    discovery_run: str
    model: str
    recorded_at: datetime
    tenant: str
    goal: str
    human_steps: int = 0


class Approval(Strict):
    state: Literal["draft", "approved", "deprecated"] = "draft"
    reviewer: str | None = None
    at: datetime | None = None
    content_hash: str | None = None
    notes: str | None = None


class Capability(Strict):
    schema_version: Literal["cua.capability/v1"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    description: str
    app: AppRef
    entry: str = Field("/login", description="Path relative to the tenant base URL where replay starts.")
    inputs: list[InputParam]
    outputs: list[OutputField]
    outcomes: list[OutcomeMapping] = []
    steps: list[Step]
    success: Checkpoint
    provenance: Provenance
    approval: Approval = Approval()

    @field_validator("steps")
    @classmethod
    def _ids_unique(cls, v: list[Step]) -> list[Step]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return v

    @model_validator(mode="after")
    def _references(self) -> Capability:
        ins = {i.name for i in self.inputs}
        outs = {o.name for o in self.outputs}
        for s in self.steps:
            if s.value and s.value.input and s.value.input not in ins:
                raise ValueError(f"{s.id}: unknown input {s.value.input}")
            if s.output and s.output not in outs:
                raise ValueError(f"{s.id}: unknown output {s.output}")
            for t in _templates_in([s.target.model_dump(), s.expect.model_dump() if s.expect else None]):
                if t not in ins:
                    raise ValueError(f"{s.id}: template {{{{{t}}}}} is not a declared input")
        for t in _templates_in(self.success.model_dump()):
            if t not in ins:
                raise ValueError(f"success: template {{{{{t}}}}} is not a declared input")
        extracted = {s.output for s in self.steps if s.action == "extract"}
        if missing := outs - extracted:
            raise ValueError(f"outputs never extracted: {sorted(missing)}")
        return self

    # -- derived views -------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def max_risk(self) -> Risk:
        order = [Risk.read, Risk.reversible, Risk.irreversible]
        return max((s.risk for s in self.steps), key=order.index, default=Risk.read)

    def content_hash(self) -> str:
        """Hash of everything that affects behaviour (not approval metadata).
        An approval is bound to this hash; editing an approved artifact voids it."""
        body = self.model_dump(mode="json", exclude={"approval"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]

    def tool_schema(self) -> dict[str, Any]:
        """Function-calling definition a calling agent can bind to."""
        props: dict[str, Any] = {}
        for p in self.inputs:
            js: dict[str, Any] = {"type": "integer" if p.type == ParamType.integer else "string", "description": p.description}
            if p.pattern:
                js["pattern"] = p.pattern
            if p.enum:
                js["enum"] = p.enum
            props[p.name] = js
        outcome_codes = ", ".join(o.code for o in self.outcomes) or "none"
        return {
            "name": self.id.replace(".", "__"),
            "description": (
                f"{self.title}. {self.description} Returns outputs "
                f"{[o.name for o in self.outputs]} on success; business outcomes: {outcome_codes}. "
                f"Risk: {self.max_risk.value}."
            ),
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.inputs if p.required],
                "additionalProperties": False,
            },
        }


def _templates_in(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(_TEMPLATE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= _templates_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _templates_in(v)
    return found


def split_location(loc: str) -> tuple[str, list[tuple[str, str]]]:
    """Path + *decoded* query pairs, so 'Holiday+Club' and 'Holiday Club' compare equal."""
    from urllib.parse import parse_qsl

    path, _, query = loc.partition("?")
    return path, parse_qsl(query, keep_blank_values=True)


def location_matches(actual: str, expected: str) -> bool:
    return split_location(actual) == split_location(expected)


def render(obj: Any, inputs: dict[str, str]) -> Any:
    """Substitute ``{{name}}`` templates in any nested structure."""
    if isinstance(obj, str):
        return _TEMPLATE.sub(lambda m: inputs[m.group(1)], obj)
    if isinstance(obj, dict):
        return {k: render(v, inputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, inputs) for v in obj]
    return obj


# --------------------------------------------------------------------------- overlays


class TargetPatch(Strict):
    step: str
    strategies: list[Strategy] | None = None
    frame: str | None = None


class Overlay(Strict):
    """A specialisation of a base capability for a vendor version or a tenant.

    Resolution order at replay: base → version overlay → tenant overlay.
    Overlays may only re-target controls and adjust checkpoints; they cannot
    add steps, change the contract, or raise risk — so an approved base plus
    a reviewed overlay is still the same capability, and the calling agent's
    contract is identical across every tenant.
    """

    applies_to: str = Field(description="capability id@version this patches")
    scope: Literal["app_version", "tenant"]
    key: str = Field(description="vendor version ('4.3') or tenant id ('pineridge')")
    reason: str
    reviewed_by: str | None = Field(None, description="Overlays are reviewed like artifacts; unreviewed ones only run in draft mode.")
    targets: list[TargetPatch] = []
    success: Checkpoint | None = None


def apply_overlay(cap: Capability, ov: Overlay) -> Capability:
    if ov.applies_to != cap.ref:
        raise ValueError(f"overlay applies to {ov.applies_to}, not {cap.ref}")
    data = cap.model_copy(deep=True)
    by_id = {s.id: s for s in data.steps}
    for p in ov.targets:
        if p.step not in by_id:
            raise ValueError(f"overlay patches unknown step {p.step}")
        t = by_id[p.step].target
        if p.strategies is not None:
            t.strategies = list(p.strategies)
        if p.frame is not None:
            t.frame = p.frame
    if ov.success is not None:
        data.success = ov.success
    return Capability.model_validate(data.model_dump())


# --------------------------------------------------------------------------- goal spec (input to discovery)


class GoalInput(InputParam):
    example: str = Field(description="Value used during the discovery run (synthetic data only).")


class GoalSpec(Strict):
    """What a capability author hands to discovery: the contract, a goal, sample values.

    The contract is human-authored on purpose: the model is good at finding
    *how*; deciding *what the calling agent may ask for and gets back* is a
    product decision."""

    capability_id: str
    title: str
    description: str
    goal: str = Field(description="Natural-language goal; {name} placeholders are filled from input examples.")
    tenant: str
    inputs: list[GoalInput]
    outputs: list[OutputField]
    max_steps: int = 25

    def rendered_goal(self) -> str:
        return self.goal.format(**{i.name: i.example for i in self.inputs})
