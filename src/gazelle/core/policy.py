"""Policy compiler and Policy Decision Point (PDP).

Pure functions, no I/O. Loads YAML once and returns a frozen PolicyBundle that
the PDP evaluates against ActionRequests.

The spec lives in docs/02-policy-language.md. This implements Tier 1 (YAML)
plus the basics of Tier 2 (predicates). Tier 3 (Python escape hatch) is a
hook point only in the MVP.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gazelle.core.types import (
    ActionRequest,
    Decision,
    ExecutionContext,
    Verdict,
    canonical_json,
)

# ---------------------------------------------------------------------------
# Compiled bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDefaults:
    on_missing_shadow: Verdict = Verdict.APPROVE_REQUIRED
    on_no_match: Verdict = Verdict.DENY


@dataclass(frozen=True)
class CompiledRule:
    id: str
    priority: int
    description: str
    matcher: Callable[[ActionRequest, ExecutionContext], bool]
    decision_factory: Callable[[ActionRequest, ExecutionContext], Decision]
    source_location: str


@dataclass(frozen=True)
class PolicyBundle:
    id: str
    version: int
    rules: tuple[CompiledRule, ...]
    defaults: PolicyDefaults
    source_files: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Public Decision constructors (used in Tier 3 Python rules and tests)
# ---------------------------------------------------------------------------


def allow(reason: str = "", matched_rules: tuple[str, ...] = ()) -> Decision:
    return Decision(verdict=Verdict.ALLOW, reason=reason, matched_rules=matched_rules)


def deny(reason: str, matched_rules: tuple[str, ...] = ()) -> Decision:
    return Decision(verdict=Verdict.DENY, reason=reason, matched_rules=matched_rules)


def dry_run(reason: str = "", matched_rules: tuple[str, ...] = ()) -> Decision:
    return Decision(verdict=Verdict.DRY_RUN, reason=reason, matched_rules=matched_rules)


def approve_required(
    approvers: tuple[str, ...] = (),
    timeout_seconds: int = 1800,
    reason: str = "",
    matched_rules: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        verdict=Verdict.APPROVE_REQUIRED,
        reason=reason,
        matched_rules=matched_rules,
        approvers=approvers,
        timeout_seconds=timeout_seconds,
    )


def transform(
    transform_args: dict[str, Any],
    reason: str = "",
    matched_rules: tuple[str, ...] = (),
) -> Decision:
    return Decision(
        verdict=Verdict.TRANSFORM,
        reason=reason,
        matched_rules=matched_rules,
        transform_args=transform_args,
    )


# ---------------------------------------------------------------------------
# Tier 3 decorator (registration only; evaluation hooked into compile)
# ---------------------------------------------------------------------------


_python_rules: list[tuple[str, int, Callable[..., Decision | None]]] = []


def rule(
    id: str | None = None,
    priority: int = 0,
) -> Callable[[Callable[..., Decision | None]], Callable[..., Decision | None]]:
    def deco(fn: Callable[..., Decision | None]) -> Callable[..., Decision | None]:
        _python_rules.append((id or fn.__name__, priority, fn))
        return fn

    return deco


def clear_python_rules() -> None:
    """Test helper."""
    _python_rules.clear()


# ---------------------------------------------------------------------------
# Matcher compilation
# ---------------------------------------------------------------------------


PathFn = Callable[[ActionRequest, ExecutionContext], Any]


def _path_getter(dotted: str) -> PathFn:
    """Resolve a dotted path against (request, context).

    Special prefixes:
        tool                              → request.tool
        args.<...>                        → request.args[<...>]
        declared.<field>                  → request.declared.<field>
        context.<field> / .principal.<f>  → request.context.<...>
    """
    parts = dotted.split(".")

    def get(req: ActionRequest, ctx: ExecutionContext) -> Any:
        if parts[0] == "tool":
            return req.tool
        if parts[0] == "args":
            cur: Any = req.args
            for p in parts[1:]:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    return None
            return cur
        if parts[0] == "declared":
            cur = req.declared
            for p in parts[1:]:
                cur = getattr(cur, p, None)
            return cur
        if parts[0] == "context":
            cur = req.context
            for p in parts[1:]:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = getattr(cur, p, None)
            return cur
        return None

    return get


_MAX_REGEX_LENGTH = 1000
# Textbook catastrophic-backtracking shapes: the inner quantified atom and the
# outer group repeat the SAME thing. e.g., (a+)+, (\w*)*, (.+)+, (a|a)+.
# We don't try to catch every ReDoS — that requires automaton analysis. We
# catch the classic shapes that almost certainly indicate a bug.
_REGEX_DANGEROUS_PATTERNS = (
    re.compile(r"\(\s*\\?[wWsSdD.]\s*[\*\+]\s*\)\s*[\*\+]"),  # (\w+)+, (.*)*, (.+)+
    re.compile(r"\(\s*[a-zA-Z0-9]\s*[\*\+]\s*\)\s*[\*\+]"),  # (a+)+, (b*)*
    re.compile(r"\(\s*([^)|]+)\s*\|\s*\1\s*\)\s*[\*\+]"),  # (a|a)+
)


def _compile_safe_regex(pattern: str) -> re.Pattern[str]:
    """Compile a regex pattern after rejecting obviously dangerous shapes.

    This is a first-pass guard against ReDoS. It is NOT a full ReDoS analyzer
    — for that we'd need an automaton-based engine. We catch the common
    catastrophic-backtracking shapes (nested unbounded quantifiers) and cap
    pattern length.
    """
    if len(pattern) > _MAX_REGEX_LENGTH:
        raise ValueError(f"Regex pattern too long ({len(pattern)} > {_MAX_REGEX_LENGTH})")
    for danger in _REGEX_DANGEROUS_PATTERNS:
        if danger.search(pattern):
            raise ValueError(
                f"Regex pattern {pattern!r} contains a nested unbounded "
                "quantifier; would be vulnerable to catastrophic backtracking"
            )
    return re.compile(pattern)


_OPERATORS = {
    "matches",
    "in",
    "contains",
    "contains_any",
    "contains_all",
    "gt",
    "ge",
    "lt",
    "le",
    "between",
    "not_between",
    "eq",
}


def _compile_predicate(
    spec: dict[str, Any] | str,
    predicates: dict[str, dict[str, Any]],
) -> Callable[[ActionRequest, ExecutionContext], bool]:
    """Compile a match-block (or reference to a named predicate)."""
    if isinstance(spec, str):
        if spec not in predicates:
            raise ValueError(f"Unknown predicate: {spec!r}")
        return _compile_predicate(predicates[spec], predicates)

    if not isinstance(spec, dict):
        raise ValueError(f"Predicate must be dict or predicate name, got: {spec!r}")

    leaves: list[Callable[[ActionRequest, ExecutionContext], bool]] = []

    for key, value in spec.items():
        if key == "all_of":
            sub = [_compile_predicate(s, predicates) for s in value]
            leaves.append(lambda r, c, sub=sub: all(p(r, c) for p in sub))
        elif key == "any_of":
            sub = [_compile_predicate(s, predicates) for s in value]
            leaves.append(lambda r, c, sub=sub: any(p(r, c) for p in sub))
        elif key == "not":
            inner = _compile_predicate(value, predicates)
            leaves.append(lambda r, c, inner=inner: not inner(r, c))
        else:
            leaves.append(_compile_leaf(key, value))

    return lambda r, c, leaves=leaves: all(p(r, c) for p in leaves)


def _compile_leaf(key: str, value: Any) -> Callable[[ActionRequest, ExecutionContext], bool]:
    """Compile a single 'field' or 'field.operator' entry."""
    if "." in key:
        head, _, tail = key.rpartition(".")
        if tail in _OPERATORS:
            getter = _path_getter(head)
            return _operator_check(getter, tail, value)
    # plain equality
    getter = _path_getter(key)
    return lambda r, c, getter=getter, value=value: getter(r, c) == value


def _operator_check(
    getter: PathFn, op: str, value: Any
) -> Callable[[ActionRequest, ExecutionContext], bool]:
    if op == "eq":
        return lambda r, c: getter(r, c) == value
    if op == "matches":
        pat = _compile_safe_regex(value)

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return isinstance(v, str) and pat.search(v) is not None

        return check
    if op == "in":
        return lambda r, c: getter(r, c) in value
    if op == "contains":

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and value in v

        return check
    if op == "contains_any":

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            if v is None:
                return False
            return any(item in v for item in value)

        return check
    if op == "contains_all":

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            if v is None:
                return False
            return all(item in v for item in value)

        return check
    if op in {"gt", "ge", "lt", "le"}:
        cmp = {
            "gt": lambda a, b: a > b,
            "ge": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "le": lambda a, b: a <= b,
        }[op]

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and cmp(v, value)

        return check
    if op == "between":
        lo, hi = value

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and lo <= v <= hi

        return check
    if op == "not_between":
        lo, hi = value

        def check(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and not (lo <= v <= hi)

        return check
    raise ValueError(f"Unknown operator: {op}")


# ---------------------------------------------------------------------------
# Decision factory compilation
# ---------------------------------------------------------------------------


def _compile_decision(
    raw: dict[str, Any] | str, rule_id: str
) -> Callable[[ActionRequest, ExecutionContext], Decision]:
    """Turn a YAML decision into a function producing a Decision."""
    if isinstance(raw, str):
        return _simple_decision(raw, rule_id)

    verdict_str = raw.get("verdict") or raw.get("decision") or "deny"
    verdict = Verdict(verdict_str) if isinstance(verdict_str, str) else verdict_str
    reason = raw.get("reason", "")
    approvers = tuple(raw.get("approvers", ()))
    timeout = raw.get("timeout_seconds")
    transform_spec = raw.get("transform")

    def factory(req: ActionRequest, ctx: ExecutionContext) -> Decision:
        return Decision(
            verdict=verdict,
            reason=reason,
            matched_rules=(rule_id,),
            approvers=approvers,
            timeout_seconds=timeout,
            transform_args=_apply_transform(transform_spec, req) if transform_spec else None,
        )

    return factory


def _simple_decision(
    name: str, rule_id: str
) -> Callable[[ActionRequest, ExecutionContext], Decision]:
    v = Verdict(name)
    return lambda r, c: Decision(verdict=v, matched_rules=(rule_id,))


def _apply_transform(spec: dict[str, Any], req: ActionRequest) -> dict[str, Any]:
    """Apply a transform specification to args. Very small for MVP."""
    new_args = dict(req.args)
    target = spec.get("jsonpath", "$.args").removeprefix("$.args.")
    if "set" in spec:
        new_args[target] = spec["set"]
    elif "append" in spec:
        cur = new_args.get(target, "")
        new_args[target] = str(cur) + str(spec["append"])
    elif "delete" in spec:
        new_args.pop(target, None)
    return new_args


# ---------------------------------------------------------------------------
# Compile entrypoint
# ---------------------------------------------------------------------------


def compile_policy(source: str | dict[str, Any], source_path: str = "<inline>") -> PolicyBundle:
    """Compile YAML text (or dict) into a PolicyBundle."""
    data: dict[str, Any]
    if isinstance(source, str):
        data = yaml.safe_load(source) or {}
    else:
        data = source

    version = int(data.get("version", 1))
    defaults_raw = data.get("defaults", {})
    defaults = PolicyDefaults(
        on_missing_shadow=Verdict(
            defaults_raw.get("on_missing_shadow", Verdict.APPROVE_REQUIRED.value)
        ),
        on_no_match=Verdict(defaults_raw.get("on_no_match", Verdict.DENY.value)),
    )

    predicates: dict[str, dict[str, Any]] = data.get("predicates", {}) or {}

    rules: list[CompiledRule] = []
    raw_rules = data.get("rules", []) or []
    for idx, rspec in enumerate(raw_rules):
        rid = rspec.get("id") or f"rule_{idx}"
        priority = int(rspec.get("priority", 0))
        description = rspec.get("description", "")
        match = rspec.get("match", {})
        matcher = _compile_predicate(match, predicates)
        decision_factory = _compile_decision(
            {
                "verdict": rspec.get("decision", "deny"),
                "reason": rspec.get("reason", ""),
                "approvers": rspec.get("approvers", []),
                "timeout_seconds": rspec.get("timeout_seconds"),
                "transform": rspec.get("transform"),
            },
            rid,
        )
        rules.append(
            CompiledRule(
                id=rid,
                priority=priority,
                description=description,
                matcher=matcher,
                decision_factory=decision_factory,
                source_location=f"{source_path}:rule[{idx}]",
            )
        )

    # Sort: priority desc, file order
    rules.sort(key=lambda r: (-r.priority, r.source_location))

    bundle_id = hashlib.sha256(
        canonical_json({"version": version, "rules": [r.id for r in rules]}).encode()
    ).hexdigest()[:16]

    return PolicyBundle(
        id=bundle_id,
        version=version,
        rules=tuple(rules),
        defaults=defaults,
        source_files=(source_path,),
    )


def load_policy_file(path: str | Path) -> PolicyBundle:
    p = Path(path)
    return compile_policy(p.read_text(), source_path=str(p))


# ---------------------------------------------------------------------------
# PDP (Policy Decision Point)
# ---------------------------------------------------------------------------


def evaluate(
    bundle: PolicyBundle,
    request: ActionRequest,
    context: ExecutionContext,
) -> Decision:
    """Evaluate a request against the bundle. Pure, deterministic.

    First-match-wins. If no rule matches, fall through to defaults.
    """
    # Tier 3: in-memory Python rules first (sorted by priority desc)
    for rule_id, _priority, fn in sorted(_python_rules, key=lambda x: -x[1]):
        result = fn(request, context)
        if result is not None:
            return Decision(
                verdict=result.verdict,
                reason=result.reason or "",
                matched_rules=(*result.matched_rules, rule_id)
                if rule_id not in result.matched_rules
                else result.matched_rules,
                approvers=result.approvers,
                transform_args=result.transform_args,
                timeout_seconds=result.timeout_seconds,
            )

    for rule in bundle.rules:
        try:
            if rule.matcher(request, context):
                return rule.decision_factory(request, context)
        except Exception:
            # A broken rule should not crash the PDP; treat as no-match.
            continue

    # Defaults
    if not request.declared.reversible and not request.declared.has_shadow:
        return Decision(
            verdict=bundle.defaults.on_missing_shadow,
            reason="irreversible action with no shadow; default policy",
            matched_rules=("<default:on_missing_shadow>",),
        )
    return Decision(
        verdict=bundle.defaults.on_no_match,
        reason="no rule matched; default policy",
        matched_rules=("<default:on_no_match>",),
    )
