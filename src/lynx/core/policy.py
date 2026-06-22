"""Policy compiler + Policy Decision Point (PDP).

Pure functions; the PDP takes (bundle, request, context) and returns a Decision.
No module-level state. Python rules are passed explicitly to ``compile_policy``;
no ``@rule`` decorator with a hidden registry.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from lynx.core.types import (
    ActionRequest,
    Decision,
    ExecutionContext,
    Verdict,
    canonical_json,
)

__all__ = [
    "Combiner",
    "LayeredPolicyBundle",
    "Policy",
    "PolicyBundle",
    "PolicyCompileError",
    "PolicyDefaults",
    "PolicyLayer",
    "PythonRule",
    "allow",
    "approve_required",
    "compile_policy",
    "deny",
    "dry_run",
    "evaluate",
    "first_layer_wins",
    "last_layer_wins",
    "load_policy_file",
    "strict_overrides_loose",
    "transform",
]


class PolicyCompileError(ValueError):
    """Raised when a policy YAML cannot be compiled into a PolicyBundle.

    Wraps PyYAML parse errors, unknown operators, malformed rules, and
    ReDoS-guard regex rejections. Catch this one type to surface a friendly
    error to operators.
    """


# ---------------------------------------------------------------------------
# Public Decision constructors (used in Python rules + tests)
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
    transform_args: Mapping[str, Any],
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
# Bundle types (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyDefaults:
    on_missing_shadow: Verdict = Verdict.APPROVE_REQUIRED
    on_no_match: Verdict = Verdict.DENY


@dataclass(frozen=True, slots=True)
class CompiledRule:
    id: str
    priority: int
    description: str
    matcher: Callable[[ActionRequest, ExecutionContext], bool]
    decision_factory: Callable[[ActionRequest, ExecutionContext], Decision]
    source_location: str
    # Sort index — used as the tie-break after -priority so file order is
    # preserved correctly past 10 rules at the same priority.
    order: int = 0


# A PythonRule is just any callable matching this shape.
PythonRule = Callable[[ActionRequest, ExecutionContext], "Decision | None"]


@dataclass(frozen=True, slots=True)
class _EvalStep:
    """One entry in the unified evaluation order (Python + YAML interleaved)."""

    rule_id: str
    priority: int
    order: int
    kind: str  # "python" | "yaml"
    fn: Callable[[ActionRequest, ExecutionContext], Decision | None]


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    id: str
    version: int
    rules: tuple[CompiledRule, ...]
    python_rules: tuple[tuple[str, int, PythonRule], ...]
    defaults: PolicyDefaults
    source_files: tuple[str, ...] = ()
    # Interleaved evaluation order — Python and YAML rules merged into a single
    # priority-ordered list so a higher-priority YAML rule beats a lower-priority
    # Python rule (and vice versa). Defaults to empty tuple for backward compat;
    # populated by ``compile_policy``.
    eval_order: tuple[_EvalStep, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Layered policy scopes
#
# Mechanism, not policy: Lynx evaluates each named layer independently and hands
# the per-layer Decisions to a developer-supplied ``Combiner``. The developer
# owns the trust model — what the layers are, their order, and how disagreements
# resolve. We ship a few combiners as batteries; none is privileged. A layer
# that matches no rule *abstains* (contributes ``None``), so an empty layer never
# forces a verdict; defaults apply only when every layer abstains.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyLayer:
    """One named layer of a layered policy. ``source`` is YAML text or a dict;
    ``name`` is any label the developer chooses (e.g. "org", "team", "user")."""

    name: str
    source: str | Mapping[str, Any]
    python_rules: tuple[PythonRule, ...] = ()


# A Combiner receives every layer's ``(name, Decision | None)`` (None = the layer
# abstained) and returns the final Decision. It is only ever called with at least
# one non-None decision — all-abstain is handled by the defaults before merge.
Combiner = Callable[[tuple[tuple[str, "Decision | None"], ...]], Decision]


@dataclass(frozen=True, slots=True)
class LayeredPolicyBundle:
    """A bundle of named layers + a developer-chosen merge strategy.

    ``defaults`` is the strictest of the layers' defaults (fail-closed), applied
    only when every layer abstains.
    """

    id: str
    layers: tuple[tuple[str, PolicyBundle], ...]
    defaults: PolicyDefaults
    merge: Combiner
    source_files: tuple[str, ...] = ()


# Either kind of compiled policy can be passed to ``evaluate`` / ``run_agent``.
Policy = PolicyBundle | LayeredPolicyBundle


# Severity ladder, most → least restrictive. Used by the shipped combiners and
# to pick the strictest defaults across layers.
_VERDICT_SEVERITY: dict[Verdict, int] = {
    Verdict.DENY: 4,
    Verdict.APPROVE_REQUIRED: 3,
    Verdict.DRY_RUN: 2,
    Verdict.TRANSFORM: 1,
    Verdict.ALLOW: 0,
}


def _decided(layers: tuple[tuple[str, Decision | None], ...]) -> list[Decision]:
    return [d for _, d in layers if d is not None]


def _with_matched_rules(decision: Decision, matched_rules: tuple[str, ...]) -> Decision:
    """A copy of ``decision`` with new ``matched_rules`` and every other field
    preserved. The single point that rebuilds a (frozen) Decision when only its
    provenance changes — so adding a Decision field never silently drops it from
    the merge / layer-tag / error-marker paths."""
    return replace(decision, matched_rules=matched_rules)


def _merge_same_verdict(decisions: list[Decision]) -> Decision:
    """Fold decisions that share one verdict into a single Decision, unioning
    their (layer-tagged) ``matched_rules`` in order; first decision wins for the
    scalar fields (reason / approvers / transform_args / timeout)."""
    matched = tuple(r for d in decisions for r in d.matched_rules)
    return _with_matched_rules(decisions[0], matched)


def strict_overrides_loose(layers: tuple[tuple[str, Decision | None], ...]) -> Decision:
    """Most-restrictive verdict wins (DENY > APPROVE_REQUIRED > DRY_RUN >
    TRANSFORM > ALLOW). Broader layers set a floor; narrower layers can only
    tighten. Ties merge provenance across all winning layers; first layer wins
    the scalar fields. The fail-closed default combiner."""
    decided = _decided(layers)
    top = max(_VERDICT_SEVERITY[d.verdict] for d in decided)
    return _merge_same_verdict([d for d in decided if _VERDICT_SEVERITY[d.verdict] == top])


def last_layer_wins(layers: tuple[tuple[str, Decision | None], ...]) -> Decision:
    """Most-specific layer is authoritative (CSS-cascade): the last non-abstaining
    layer's decision wins outright — it can re-grant what a broader layer denied."""
    return _decided(layers)[-1]


def first_layer_wins(layers: tuple[tuple[str, Decision | None], ...]) -> Decision:
    """Broadest layer is authoritative: the first non-abstaining layer wins."""
    return _decided(layers)[0]


# ---------------------------------------------------------------------------
# Matcher compilation
# ---------------------------------------------------------------------------


PathFn = Callable[[ActionRequest, ExecutionContext], Any]


def _path_getter(dotted: str) -> PathFn:
    parts = dotted.split(".")

    def get(req: ActionRequest, ctx: ExecutionContext) -> Any:
        if parts[0] == "tool":
            return req.tool
        if parts[0] == "args":
            cur: Any = req.args
            for p in parts[1:]:
                if isinstance(cur, Mapping):
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
                if isinstance(cur, Mapping):
                    cur = cur.get(p)
                else:
                    cur = getattr(cur, p, None)
            return cur
        return None

    return get


_MAX_REGEX_LENGTH = 1000
_REGEX_DANGEROUS_PATTERNS = (
    re.compile(r"\(\s*\\?[wWsSdD.]\s*[\*\+]\s*\)\s*[\*\+]"),
    re.compile(r"\(\s*[a-zA-Z0-9]\s*[\*\+]\s*\)\s*[\*\+]"),
    re.compile(r"\(\s*([^)|]+)\s*\|\s*\1\s*\)\s*[\*\+]"),
)


def _compile_safe_regex(pattern: str) -> re.Pattern[str]:
    if len(pattern) > _MAX_REGEX_LENGTH:
        raise PolicyCompileError(f"Regex pattern too long ({len(pattern)} > {_MAX_REGEX_LENGTH})")
    for danger in _REGEX_DANGEROUS_PATTERNS:
        if danger.search(pattern):
            raise PolicyCompileError(
                f"Regex pattern {pattern!r} contains a nested unbounded "
                "quantifier; would be vulnerable to catastrophic backtracking"
            )
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise PolicyCompileError(f"Invalid regex {pattern!r}: {exc}") from exc


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
    spec: Mapping[str, Any] | str,
    predicates: Mapping[str, Mapping[str, Any]],
) -> Callable[[ActionRequest, ExecutionContext], bool]:
    if isinstance(spec, str):
        if spec not in predicates:
            suggestion = difflib.get_close_matches(spec, list(predicates), n=1, cutoff=0.6)
            hint = f" (did you mean {suggestion[0]!r}?)" if suggestion else ""
            raise PolicyCompileError(f"Unknown predicate: {spec!r}{hint}")
        return _compile_predicate(predicates[spec], predicates)

    if not isinstance(spec, Mapping):
        raise PolicyCompileError(f"Predicate must be Mapping or predicate name, got: {spec!r}")

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
    if "." in key:
        head, _, tail = key.rpartition(".")
        if tail in _OPERATORS:
            getter = _path_getter(head)
            return _operator_check(getter, tail, value)
        # Operator-shaped typo guard: a trailing segment that is a close miss
        # of a known operator is almost certainly a typo, not a literal field
        # name. Silent-fail would just be a never-matching rule.
        suggestion = difflib.get_close_matches(tail, sorted(_OPERATORS), n=1, cutoff=0.75)
        if suggestion:
            raise PolicyCompileError(
                f"Unknown operator suffix on {key!r}: "
                f"{tail!r} looks like a typo of {suggestion[0]!r}. "
                f"Known operators: {sorted(_OPERATORS)}"
            )
    getter = _path_getter(key)
    return lambda r, c, getter=getter, value=value: getter(r, c) == value


def _operator_check(
    getter: PathFn, op: str, value: Any
) -> Callable[[ActionRequest, ExecutionContext], bool]:
    if op == "eq":
        return lambda r, c: getter(r, c) == value
    if op == "matches":
        pat = _compile_safe_regex(value)

        def check_matches(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return isinstance(v, str) and pat.search(v) is not None

        return check_matches
    if op == "in":
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise PolicyCompileError(
                f"`in` operator requires a list/tuple/set on the right-hand side, "
                f"got {type(value).__name__}: {value!r}"
            )
        rhs = (
            frozenset(value)
            if all(isinstance(x, (str, int, float, bool)) for x in value)
            else tuple(value)
        )
        return lambda r, c: getter(r, c) in rhs
    if op == "contains":

        def check_contains(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and value in v

        return check_contains
    if op == "contains_any":

        def check_contains_any(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            if v is None:
                return False
            return any(item in v for item in value)

        return check_contains_any
    if op == "contains_all":

        def check_contains_all(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            if v is None:
                return False
            return all(item in v for item in value)

        return check_contains_all
    if op in {"gt", "ge", "lt", "le"}:
        cmp_fn = {
            "gt": lambda a, b: a > b,
            "ge": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "le": lambda a, b: a <= b,
        }[op]

        def check_cmp(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and cmp_fn(v, value)

        return check_cmp
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise PolicyCompileError(
                f"`between` operator requires a 2-element list/tuple [lo, hi], got: {value!r}"
            )
        lo, hi = value
        if lo > hi:
            raise PolicyCompileError(f"`between` operator: lo > hi ({lo} > {hi}); range is empty")

        def check_between(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and lo <= v <= hi

        return check_between
    if op == "not_between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise PolicyCompileError(
                f"`not_between` operator requires a 2-element list/tuple [lo, hi], got: {value!r}"
            )
        lo, hi = value

        def check_not_between(r: ActionRequest, c: ExecutionContext) -> bool:
            v = getter(r, c)
            return v is not None and not (lo <= v <= hi)

        return check_not_between
    raise PolicyCompileError(f"Unknown operator: {op}")


# ---------------------------------------------------------------------------
# Decision factory compilation
# ---------------------------------------------------------------------------


def _parse_verdict(value: Any, rule_id: str) -> Verdict:
    """Verdict() is case-sensitive ('allow' only). Accept upper-case in YAML."""
    if isinstance(value, Verdict):
        return value
    if not isinstance(value, str):
        raise PolicyCompileError(
            f"Rule {rule_id!r}: verdict must be a string, got {type(value).__name__}"
        )
    try:
        return Verdict(value.lower())
    except ValueError as exc:
        valid = [v.value for v in Verdict]
        raise PolicyCompileError(
            f"Rule {rule_id!r}: unknown verdict {value!r}; valid: {valid}"
        ) from exc


def _compile_decision(
    raw: Mapping[str, Any] | str, rule_id: str
) -> Callable[[ActionRequest, ExecutionContext], Decision]:
    if isinstance(raw, str):
        return _simple_decision(raw, rule_id)

    verdict_str = raw.get("verdict") or raw.get("decision") or "deny"
    verdict = _parse_verdict(verdict_str, rule_id)
    reason = raw.get("reason", "")
    approvers = tuple(raw.get("approvers", ()))
    timeout = raw.get("timeout_seconds")
    transform_spec = raw.get("transform")

    if verdict == Verdict.TRANSFORM and transform_spec is None:
        raise PolicyCompileError(
            f"Rule {rule_id!r}: decision is 'transform' but no `transform:` block given. "
            "A transform rule must specify at least one of set/append/delete."
        )
    if verdict != Verdict.TRANSFORM and transform_spec is not None:
        raise PolicyCompileError(
            f"Rule {rule_id!r}: `transform:` block only applies to a transform decision"
        )
    if transform_spec is not None:
        _validate_transform_spec(transform_spec, rule_id)

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
    v = _parse_verdict(name, rule_id)
    if v == Verdict.TRANSFORM:
        raise PolicyCompileError(
            f"Rule {rule_id!r}: short-form `decision: transform` is not allowed; "
            "transform rules need an explicit `transform:` block."
        )
    return lambda r, c: Decision(verdict=v, matched_rules=(rule_id,))


_TRANSFORM_OPS = {"set", "append", "delete"}


def _validate_transform_spec(spec: Mapping[str, Any], rule_id: str) -> None:
    if not isinstance(spec, Mapping):
        raise PolicyCompileError(
            f"Rule {rule_id!r}: transform must be a mapping, got {type(spec).__name__}"
        )
    used = _TRANSFORM_OPS & set(spec)
    if not used:
        raise PolicyCompileError(
            f"Rule {rule_id!r}: transform must declare at least one of {sorted(_TRANSFORM_OPS)}"
        )
    if len(used) > 1:
        raise PolicyCompileError(
            f"Rule {rule_id!r}: transform may declare only one of {sorted(used)} per rule"
        )
    jsonpath = spec.get("jsonpath", "$.args")
    if not isinstance(jsonpath, str) or not jsonpath.startswith("$.args"):
        raise PolicyCompileError(
            f"Rule {rule_id!r}: transform jsonpath must start with '$.args' "
            f"(got {jsonpath!r}). Only top-level `args.<key>` rewrites are supported."
        )


def _apply_transform(spec: Mapping[str, Any], req: ActionRequest) -> Mapping[str, Any]:
    new_args: dict[str, Any] = dict(req.args)
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


def compile_policy(
    source: str | Mapping[str, Any] | Sequence[PolicyLayer],
    source_path: str = "<inline>",
    *,
    python_rules: tuple[PythonRule, ...] = (),
    python_rule_priorities: tuple[tuple[str, int], ...] = (),
    merge: Combiner | None = None,
) -> PolicyBundle | LayeredPolicyBundle:
    """Compile YAML (or dict) into a frozen PolicyBundle.

    Python rules are passed in explicitly — no module-level registry.
    Each Python rule must be a callable ``(ActionRequest, ExecutionContext) -> Decision | None``.

    Pass a list of :class:`PolicyLayer` instead of a single source to build a
    :class:`LayeredPolicyBundle`: each layer is compiled independently and the
    per-layer decisions are combined by ``merge`` (a developer-supplied
    :data:`Combiner`; defaults to the fail-closed :func:`strict_overrides_loose`).

    Raises :class:`PolicyCompileError` for any malformed input.
    """
    if isinstance(source, (list, tuple)):
        if not source:
            raise PolicyCompileError("compile_policy([...]) requires at least one PolicyLayer")
        if not all(isinstance(x, PolicyLayer) for x in source):
            raise PolicyCompileError(
                "layered compile_policy expects a list of PolicyLayer instances"
            )
        if python_rules or python_rule_priorities:
            raise PolicyCompileError(
                "pass python_rules per layer (PolicyLayer.python_rules), not to "
                "the layered compile_policy call"
            )
        return _compile_layered(tuple(source), merge=merge or strict_overrides_loose)
    if merge is not None:
        raise PolicyCompileError("merge= is only valid when compiling a list of PolicyLayer")
    if isinstance(source, str):
        try:
            loaded = yaml.safe_load(source) or {}
        except yaml.YAMLError as exc:
            raise PolicyCompileError(f"YAML parse error: {exc}") from exc
    else:
        loaded = source
    if not isinstance(loaded, Mapping):
        raise PolicyCompileError(f"Policy root must be a mapping, got {type(loaded).__name__}")
    data: Mapping[str, Any] = loaded

    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise PolicyCompileError(
            f"version must be an integer, got {data.get('version')!r}"
        ) from exc

    defaults_raw = data.get("defaults", {}) or {}
    defaults = PolicyDefaults(
        on_missing_shadow=_parse_verdict(
            defaults_raw.get("on_missing_shadow", Verdict.APPROVE_REQUIRED.value),
            "<defaults.on_missing_shadow>",
        ),
        on_no_match=_parse_verdict(
            defaults_raw.get("on_no_match", Verdict.DENY.value),
            "<defaults.on_no_match>",
        ),
    )

    predicates: Mapping[str, Mapping[str, Any]] = data.get("predicates", {}) or {}

    rules: list[CompiledRule] = []
    rule_bodies_canonical: list[Any] = []  # for content-addressing bundle_id
    raw_rules = data.get("rules", []) or []
    for idx, rspec in enumerate(raw_rules):
        if not isinstance(rspec, Mapping):
            raise PolicyCompileError(f"rules[{idx}] must be a mapping, got {type(rspec).__name__}")
        rid = rspec.get("id") or f"rule_{idx}"
        try:
            priority = int(rspec.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise PolicyCompileError(
                f"Rule {rid!r}: priority must be an integer, got {rspec.get('priority')!r}"
            ) from exc
        description = rspec.get("description", "")
        match = rspec.get("match", {})
        matcher = _compile_predicate(match, predicates)
        decision_factory = _compile_decision(
            {
                "verdict": rspec.get("decision", rspec.get("verdict", "deny")),
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
                order=idx,
            )
        )
        rule_bodies_canonical.append(
            {
                "id": rid,
                "priority": priority,
                "match": _canonical_predicate(match, predicates),
                "decision": {
                    "verdict": _verdict_canonical(
                        rspec.get("decision", rspec.get("verdict", "deny"))
                    ),
                    "approvers": list(rspec.get("approvers", []) or []),
                    "timeout_seconds": rspec.get("timeout_seconds"),
                    "transform": dict(rspec.get("transform") or {}),
                    "reason": rspec.get("reason", ""),
                },
            }
        )

    # Stable sort: priority desc, then file order (integer).
    rules.sort(key=lambda r: (-r.priority, r.order))

    # Python rule priorities: default 0; user can override via the second tuple.
    priority_map: Mapping[str, int] = dict(python_rule_priorities)
    py_rules_compiled: tuple[tuple[str, int, PythonRule], ...] = tuple(
        sorted(
            ((fn.__name__, priority_map.get(fn.__name__, 0), fn) for fn in python_rules),
            key=lambda t: -t[1],
        )
    )

    # Unified evaluation order — interleaved by priority. Python rules return
    # Decision | None (None = abstain); YAML rules match-and-decide via the
    # returned _yaml_eval closures (None = no-match).
    eval_steps: list[_EvalStep] = []
    for r in rules:
        eval_steps.append(
            _EvalStep(
                rule_id=r.id,
                priority=r.priority,
                order=r.order,
                kind="yaml",
                fn=_make_yaml_eval(r),
            )
        )
    for py_order, (name, prio, fn) in enumerate(py_rules_compiled):
        eval_steps.append(
            _EvalStep(
                rule_id=name,
                priority=prio,
                # Python rules sort *after* equal-priority YAML rules for stability.
                order=10**9 + py_order,
                kind="python",
                fn=_make_python_eval(name, fn),
            )
        )
    eval_steps.sort(key=lambda s: (-s.priority, s.order))

    # Content-addressed bundle id — covers rule bodies, defaults, version,
    # and python-rule names+priorities.
    bundle_id = hashlib.sha256(
        canonical_json(
            {
                "version": version,
                "defaults": {
                    "on_missing_shadow": defaults.on_missing_shadow.value,
                    "on_no_match": defaults.on_no_match.value,
                },
                "rules": rule_bodies_canonical,
                "python_rules": [
                    {"name": name, "priority": prio} for name, prio, _ in py_rules_compiled
                ],
            }
        ).encode()
    ).hexdigest()[:16]

    return PolicyBundle(
        id=bundle_id,
        version=version,
        rules=tuple(rules),
        python_rules=py_rules_compiled,
        defaults=defaults,
        source_files=(source_path,),
        eval_order=tuple(eval_steps),
    )


def _strictest_verdict(verdicts: Iterable[Verdict]) -> Verdict:
    """The most-restrictive verdict in the iterable (fail-closed default merge)."""
    return max(verdicts, key=lambda v: _VERDICT_SEVERITY[v])


def _compile_layered(
    layers: tuple[PolicyLayer, ...], *, merge: Combiner
) -> LayeredPolicyBundle:
    compiled: list[tuple[str, PolicyBundle]] = []
    seen: set[str] = set()
    for layer in layers:
        if not layer.name:
            raise PolicyCompileError("PolicyLayer.name must be a non-empty string")
        if layer.name in seen:
            raise PolicyCompileError(f"duplicate layer name: {layer.name!r}")
        seen.add(layer.name)
        sub = compile_policy(
            layer.source,
            source_path=f"<layer:{layer.name}>",
            python_rules=layer.python_rules,
        )
        # Each layer source is a single policy, never itself layered.
        assert isinstance(sub, PolicyBundle)
        compiled.append((layer.name, sub))

    # Combined defaults: strictest across layers, so a forgotten layer can never
    # loosen the floor. Applied only when every layer abstains.
    combined_defaults = PolicyDefaults(
        on_missing_shadow=_strictest_verdict(b.defaults.on_missing_shadow for _, b in compiled),
        on_no_match=_strictest_verdict(b.defaults.on_no_match for _, b in compiled),
    )

    # Content-addressed id: ordered (layer name, sub-bundle id) + merge identity
    # + combined defaults. The merge callable is identified by name (like Python
    # rules) so the id is stable across processes.
    layered_id = hashlib.sha256(
        canonical_json(
            {
                "layers": [{"name": n, "id": b.id} for n, b in compiled],
                "merge": getattr(merge, "__name__", repr(merge)),
                "defaults": {
                    "on_missing_shadow": combined_defaults.on_missing_shadow.value,
                    "on_no_match": combined_defaults.on_no_match.value,
                },
            }
        ).encode()
    ).hexdigest()[:16]

    return LayeredPolicyBundle(
        id=layered_id,
        layers=tuple(compiled),
        defaults=combined_defaults,
        merge=merge,
        source_files=tuple(f"<layer:{n}>" for n, _ in compiled),
    )


def _make_yaml_eval(
    rule: CompiledRule,
) -> Callable[[ActionRequest, ExecutionContext], Decision | None]:
    def step(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        if rule.matcher(req, ctx):
            return rule.decision_factory(req, ctx)
        return None

    return step


def _make_python_eval(
    name: str, fn: PythonRule
) -> Callable[[ActionRequest, ExecutionContext], Decision | None]:
    def step(req: ActionRequest, ctx: ExecutionContext) -> Decision | None:
        result = fn(req, ctx)
        if result is None:
            return None
        # Tag the python rule name in matched_rules.
        new_matched: tuple[str, ...] = (
            result.matched_rules if name in result.matched_rules else (*result.matched_rules, name)
        )
        return Decision(
            verdict=result.verdict,
            reason=result.reason or "",
            matched_rules=new_matched,
            approvers=result.approvers,
            transform_args=result.transform_args,
            timeout_seconds=result.timeout_seconds,
        )

    return step


def _verdict_canonical(value: Any) -> str:
    if isinstance(value, Verdict):
        return value.value
    if isinstance(value, str):
        return value.lower()
    return str(value)


def _canonical_predicate(spec: Any, predicates: Mapping[str, Mapping[str, Any]]) -> Any:
    """Inline named predicates so bundle_id hashes the same thing for two
    policies that compile to equivalent matchers."""
    if isinstance(spec, str):
        if spec in predicates:
            return _canonical_predicate(predicates[spec], predicates)
        # Not a predicate name — treat as a literal string value.
        return spec
    if isinstance(spec, Mapping):
        return {k: _canonical_predicate(v, predicates) for k, v in sorted(spec.items())}
    if isinstance(spec, (list, tuple)):
        return [_canonical_predicate(v, predicates) for v in spec]
    return spec


def load_policy_file(
    path: str | Path, *, python_rules: tuple[PythonRule, ...] = ()
) -> PolicyBundle:
    p = Path(path)
    try:
        text = p.read_text()
    except OSError as exc:
        raise PolicyCompileError(f"Cannot read policy file {p}: {exc}") from exc
    bundle = compile_policy(text, source_path=str(p), python_rules=python_rules)
    # Text source is always a single policy, never layered.
    assert isinstance(bundle, PolicyBundle)
    return bundle


# ---------------------------------------------------------------------------
# PDP — pure
# ---------------------------------------------------------------------------


def _run_rules(
    bundle: PolicyBundle,
    request: ActionRequest,
    context: ExecutionContext,
) -> tuple[Decision | None, list[str]]:
    """Run one bundle's rules in priority order. Returns ``(decision, errors)``
    where ``decision`` is ``None`` if no rule matched (defaults are NOT applied —
    that's the caller's job, so a layer can abstain). ``errors`` are diagnostic
    markers for rules that raised; a buggy rule never silently fails-open."""
    eval_order = bundle.eval_order or _legacy_eval_order(bundle)
    errors: list[str] = []
    for step in eval_order:
        try:
            result = step.fn(request, context)
        except Exception as exc:
            errors.append(f"<rule_error:{step.rule_id}:{type(exc).__name__}>")
            continue
        if result is not None:
            return result, errors
    return None, errors


def evaluate(
    bundle: PolicyBundle | LayeredPolicyBundle,
    request: ActionRequest,
    context: ExecutionContext,
) -> Decision:
    """Pure: same (bundle, request, context) always returns the same Decision.

    For a single bundle, Python and YAML rules are interleaved by priority. For a
    :class:`LayeredPolicyBundle`, each layer is evaluated independently and the
    results combined by the bundle's ``merge`` strategy.
    """
    if isinstance(bundle, LayeredPolicyBundle):
        return _evaluate_layered(bundle, request, context)

    result, errors = _run_rules(bundle, request, context)
    if result is not None:
        if errors:
            return _with_matched_rules(result, (*errors, *result.matched_rules))
        return result

    return _apply_defaults(
        bundle.defaults,
        request,
        errors=tuple(errors),
        missing_shadow_reason="irreversible action with no shadow; default policy",
        no_match_reason="no rule matched; default policy",
    )


def _apply_defaults(
    defaults: PolicyDefaults,
    request: ActionRequest,
    *,
    errors: tuple[str, ...],
    missing_shadow_reason: str,
    no_match_reason: str,
) -> Decision:
    """The fail-closed default when nothing matched, shared by the single-bundle
    and layered paths so the safety floor can never drift between them: the
    ``on_missing_shadow`` floor when the action is neither reversible nor
    previewable, else ``on_no_match``."""
    if not request.declared.reversible and not request.declared.has_shadow:
        return Decision(
            verdict=defaults.on_missing_shadow,
            reason=missing_shadow_reason,
            matched_rules=(*errors, "<default:on_missing_shadow>"),
        )
    return Decision(
        verdict=defaults.on_no_match,
        reason=no_match_reason,
        matched_rules=(*errors, "<default:on_no_match>"),
    )


def _evaluate_layered(
    bundle: LayeredPolicyBundle,
    request: ActionRequest,
    context: ExecutionContext,
) -> Decision:
    """Evaluate each layer independently, layer-tag its provenance, and hand the
    per-layer decisions to the developer's ``merge`` combiner. A layer that
    matches no rule abstains (``None``); defaults apply only if every layer
    abstains. Rule-error markers are layer-tagged and threaded into the result."""
    layer_results: list[tuple[str, Decision | None]] = []
    errors: list[str] = []
    for name, sub in bundle.layers:
        result, sub_errors = _run_rules(sub, request, context)
        errors.extend(f"{name}:{e}" for e in sub_errors)
        if result is None:
            layer_results.append((name, None))
        else:
            layer_results.append((name, _tag_layer(name, result)))

    if all(decision is None for _, decision in layer_results):
        return _apply_defaults(
            bundle.defaults,
            request,
            errors=tuple(errors),
            missing_shadow_reason=(
                "irreversible action with no shadow; no layer matched; default policy"
            ),
            no_match_reason="no layer matched; default policy",
        )

    final = bundle.merge(tuple(layer_results))
    if errors:
        return _with_matched_rules(final, (*errors, *final.matched_rules))
    return final


def _tag_layer(name: str, decision: Decision) -> Decision:
    """Prefix each matched-rule id with the layer name for audit provenance."""
    return _with_matched_rules(
        decision, tuple(f"{name}:{r}" for r in decision.matched_rules)
    )


def _legacy_eval_order(bundle: PolicyBundle) -> tuple[_EvalStep, ...]:
    """Backward-compat fallback for bundles built before the eval_order field
    was added. New bundles always carry eval_order populated by compile_policy."""
    steps: list[_EvalStep] = []
    for r in bundle.rules:
        steps.append(
            _EvalStep(
                rule_id=r.id,
                priority=r.priority,
                order=r.order,
                kind="yaml",
                fn=_make_yaml_eval(r),
            )
        )
    for idx, (name, prio, fn) in enumerate(bundle.python_rules):
        steps.append(
            _EvalStep(
                rule_id=name,
                priority=prio,
                order=10**9 + idx,
                kind="python",
                fn=_make_python_eval(name, fn),
            )
        )
    steps.sort(key=lambda s: (-s.priority, s.order))
    return tuple(steps)
