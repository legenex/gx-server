"""Structured call state: the motor vehicle accident schema, validation and local reference data.

The authoritative call state lives in the application database
(``call_state``), never in the model's context. Tools and API clients change
it only through :func:`apply_fields`, which validates every value against the
agent's JSON Schema subset below and reports what is still missing.

Supported JSON Schema subset (enough for intake forms, checked server-side):
``type`` object/string/integer/number/boolean/array, ``enum``, ``format``
(date, email, phone, us_state), ``minLength``/``maxLength``, ``minimum``/
``maximum``, ``pattern``, ``items`` (one level), ``properties`` (one level of
nesting inside an object field), ``description``, ``title``.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Stored schema id of the motor vehicle accident intake template. It is a
#: persisted identifier on existing call state, so it never changes; the
#: user-facing title below is the product-neutral one.
MVA_SCHEMA_ID = "intakepilot.mva.v1"

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_BY_NAME = {v.lower(): k for k, v in US_STATES.items()}

MVA_SCHEMA: dict = {
    "$id": MVA_SCHEMA_ID,
    "title": "Motor vehicle accident intake",
    "type": "object",
    "properties": {
        "caller_name": {"type": "string", "title": "Caller name", "minLength": 2, "maxLength": 120},
        "phone": {"type": "string", "title": "Phone", "format": "phone"},
        "email": {"type": "string", "title": "Email", "format": "email"},
        "accident_date": {"type": "string", "title": "Accident date", "format": "date",
                          "description": "YYYY-MM-DD, not in the future"},
        "accident_state": {"type": "string", "title": "Accident state", "format": "us_state"},
        "accident_city": {"type": "string", "title": "City", "maxLength": 120},
        "accident_location": {"type": "string", "title": "Location", "maxLength": 300},
        "accident_type": {"type": "string", "title": "Accident type",
                          "enum": ["rear_end", "head_on", "t_bone", "sideswipe", "rollover", "pedestrian",
                                   "bicycle", "motorcycle", "truck", "rideshare", "hit_and_run", "multi_vehicle",
                                   "single_vehicle", "other"]},
        "fault": {"type": "string", "title": "Fault", "enum": ["other_driver", "caller", "shared", "unknown"]},
        "injuries": {"type": "string", "title": "Injuries", "maxLength": 600},
        "injury_severity": {"type": "string", "title": "Injury severity",
                            "enum": ["none", "minor", "moderate", "severe", "fatal", "unknown"]},
        "treatment": {"type": "string", "title": "Treatment",
                      "enum": ["none", "er", "hospitalized", "urgent_care", "doctor", "chiropractor",
                               "physical_therapy", "surgery", "planned", "other"]},
        "hospital": {"type": "string", "title": "Hospital", "maxLength": 200},
        "doctor": {"type": "string", "title": "Doctor", "maxLength": 200},
        "attorney_status": {"type": "string", "title": "Attorney status",
                            "enum": ["none", "has_attorney", "had_attorney", "consulting", "unknown"]},
        "insurance": {"type": "string", "title": "Caller insurance", "maxLength": 200},
        "other_driver_insurance": {"type": "string", "title": "Other driver insurance", "maxLength": 200},
        "police_report": {"type": "string", "title": "Police report", "enum": ["yes", "no", "unknown"]},
        "police_report_number": {"type": "string", "title": "Report number", "maxLength": 60},
        "vehicle_details": {"type": "string", "title": "Vehicle details", "maxLength": 400},
        "passengers": {"type": "integer", "title": "Passengers", "minimum": 0, "maximum": 60},
        "qualification_status": {"type": "string", "title": "Qualification",
                                 "enum": ["unknown", "qualified", "not_qualified", "needs_review"]},
        "disposition": {"type": "string", "title": "Disposition",
                        "enum": ["in_progress", "intake_complete", "transferred", "callback_requested",
                                 "not_interested", "wrong_number", "disqualified", "voicemail", "abandoned"]},
        "transfer_status": {"type": "string", "title": "Transfer",
                            "enum": ["none", "requested", "connected", "failed", "cancelled"]},
        "callback_time": {"type": "string", "title": "Callback time", "maxLength": 80},
        "notes": {"type": "string", "title": "Notes", "maxLength": 2000},
    },
}
MVA_REQUIRED_DEFAULT = ["caller_name", "phone", "accident_date", "accident_state", "injuries", "fault",
                        "attorney_status"]
MVA_OPTIONAL_DEFAULT = ["email", "accident_city", "accident_location", "accident_type", "treatment", "hospital",
                        "doctor", "insurance", "other_driver_insurance", "police_report", "vehicle_details",
                        "passengers", "notes"]
SYSTEM_FIELDS = ("qualification_status", "disposition", "transfer_status")

WC_SCHEMA_ID = "gx.workers_comp.v1"
WC_SCHEMA: dict = {
    "$id": WC_SCHEMA_ID,
    "title": "Workers compensation intake",
    "type": "object",
    "properties": {
        "caller_name": {"type": "string", "title": "Caller name", "minLength": 2, "maxLength": 120},
        "phone": {"type": "string", "title": "Phone", "format": "phone"},
        "email": {"type": "string", "title": "Email", "format": "email"},
        "accident_state": {"type": "string", "title": "State", "format": "us_state"},
        "employer_name": {"type": "string", "title": "Employer", "maxLength": 200},
        "job_role": {"type": "string", "title": "Job role", "maxLength": 200},
        "injury_date": {"type": "string", "title": "Injury date", "format": "date"},
        "injury_type": {"type": "string", "title": "Injury type", "maxLength": 200},
        "injury_description": {"type": "string", "title": "Injury description", "maxLength": 600},
        "how_occurred": {"type": "string", "title": "How it happened", "maxLength": 600},
        "happened_at_work": {"type": "string", "title": "Happened while working",
                             "enum": ["yes", "no", "unknown"]},
        "employer_notified": {"type": "string", "title": "Employer notified",
                              "enum": ["yes", "no", "unknown"]},
        "incident_report": {"type": "string", "title": "Incident report",
                            "enum": ["yes", "no", "unknown"]},
        "treatment": {"type": "string", "title": "Medical treatment",
                      "enum": ["none", "er", "hospitalized", "urgent_care", "doctor", "other", "unknown"]},
        "hospital": {"type": "string", "title": "Hospital or clinic", "maxLength": 200},
        "doctor": {"type": "string", "title": "Doctor", "maxLength": 200},
        "work_status": {"type": "string", "title": "Current work status",
                        "enum": ["working", "off_work", "light_duty", "terminated", "unknown"]},
        "missed_work": {"type": "string", "title": "Missed work", "enum": ["yes", "no", "unknown"]},
        "claim_filed": {"type": "string", "title": "Claim filed", "enum": ["yes", "no", "unknown"]},
        "claim_status": {"type": "string", "title": "Claim status",
                         "enum": ["none", "pending", "accepted", "denied", "disputed", "unknown"]},
        "wc_insurance": {"type": "string", "title": "Workers comp insurance", "maxLength": 200},
        "attorney_status": {"type": "string", "title": "Attorney status",
                            "enum": ["none", "has_attorney", "had_attorney", "consulting", "unknown"]},
        "callback_time": {"type": "string", "title": "Best callback time", "maxLength": 80},
        "consent_followup": {"type": "string", "title": "Consent to follow up",
                             "enum": ["yes", "no", "unknown"]},
        "notes": {"type": "string", "title": "Notes", "maxLength": 2000},
        "qualification_status": {"type": "string", "title": "Qualification",
                                 "enum": ["unknown", "qualified", "not_qualified", "needs_review"]},
        "disposition": {"type": "string", "title": "Disposition",
                        "enum": ["in_progress", "intake_complete", "transferred", "callback_requested",
                                 "not_interested", "wrong_number", "disqualified", "voicemail", "abandoned"]},
        "transfer_status": {"type": "string", "title": "Transfer",
                            "enum": ["none", "requested", "connected", "failed", "cancelled"]},
    },
}
WC_REQUIRED_DEFAULT = ["caller_name", "phone", "accident_state", "employer_name", "injury_date",
                       "injury_description", "happened_at_work", "attorney_status", "consent_followup"]
WC_OPTIONAL_DEFAULT = ["email", "job_role", "injury_type", "how_occurred", "employer_notified",
                       "incident_report", "treatment", "hospital", "doctor", "work_status", "missed_work",
                       "claim_filed", "claim_status", "wc_insurance", "callback_time", "notes"]


class StateError(ValueError):
    pass


def schema_fields(schema: dict) -> dict[str, dict]:
    props = schema.get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


_TYPES = {"object", "string", "integer", "number", "boolean", "array"}
_SCHEMA_KEYS = {"type", "title", "description", "enum", "format", "minLength", "maxLength", "minimum",
                "maximum", "pattern", "items", "properties", "default"}


def check_schema(schema: object, *, depth: int = 0) -> dict:
    """Validate a user-supplied structured-output schema (the supported subset)."""
    if not isinstance(schema, dict):
        raise StateError("the structured output schema must be a JSON object")
    if len(json.dumps(schema)) > 32 * 1024:
        raise StateError("the structured output schema is larger than 32 KiB")
    unknown = set(schema) - _SCHEMA_KEYS - {"$id", "$schema", "required"}
    if unknown:
        raise StateError(f"unsupported schema keywords: {sorted(unknown)}")
    kind = schema.get("type", "object" if depth == 0 else None)
    if kind not in _TYPES:
        raise StateError(f"unsupported schema type {kind!r}")
    if kind == "object":
        props = schema.get("properties", {})
        if not isinstance(props, dict) or (depth == 0 and not props) or len(props) > 80:
            raise StateError("an object schema needs 1-80 properties")
        for name, sub in props.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", name):
                raise StateError(f"invalid field name {name!r} (lower_snake_case)")
            if depth >= 1:
                raise StateError("nesting deeper than one object level is not supported")
            check_schema(sub, depth=depth + 1)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]
                             or len(schema["enum"]) > 60):
        raise StateError("enum must be a list of 1-60 values")
    if "pattern" in schema:
        try:
            re.compile(str(schema["pattern"]))
        except re.error as exc:
            raise StateError(f"invalid pattern: {exc}") from None
        if len(str(schema["pattern"])) > 200:
            raise StateError("pattern is longer than 200 characters")
    if kind == "array" and "items" in schema:
        check_schema(schema["items"], depth=max(depth, 1))
    return schema


def normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1{digits}"
    if 8 <= len(digits) <= 15 and value.strip().startswith("+"):
        return "+" + digits
    return None


def normalize_state(value: str) -> str | None:
    v = value.strip()
    if v.upper() in US_STATES:
        return v.upper()
    return _STATE_BY_NAME.get(v.lower())


def _date(value: str, today: dt.date) -> str:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            d = dt.datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise StateError("use a date like 2026-09-01")
    if d > today:
        raise StateError("the date is in the future")
    if d < today - dt.timedelta(days=365 * 30):
        raise StateError("the date is implausibly old")
    return d.isoformat()


def coerce(name: str, spec: dict, value: Any, *, today: dt.date | None = None) -> Any:
    """Validate and normalise one value; raises StateError with a speakable reason."""
    today = today or dt.date.today()
    kind = spec.get("type", "string")
    if value is None:
        return None
    if kind == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise StateError("must be text")
        value = " ".join(value.split())
        fmt = spec.get("format")
        if "enum" in spec:
            cand = value.lower().replace(" ", "_").replace("-", "_")
            if cand not in spec["enum"]:
                raise StateError(f"must be one of {', '.join(map(str, spec['enum']))}")
            value = cand
        elif fmt == "phone":
            norm = normalize_phone(value)
            if norm is None:
                raise StateError("needs a 10-digit US phone number or an international number with +")
            value = norm
        elif fmt == "email":
            value = value.lower().replace(" at ", "@").replace(" dot ", ".").replace(" ", "")
            if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", value):
                raise StateError("is not a valid email address")
        elif fmt == "date":
            value = _date(value, today)
        elif fmt == "us_state":
            norm = normalize_state(value)
            if norm is None:
                raise StateError("must be a US state name or two-letter code")
            value = norm
        if "minLength" in spec and len(value) < int(spec["minLength"]):
            raise StateError(f"must be at least {spec['minLength']} characters")
        if "maxLength" in spec and len(value) > int(spec["maxLength"]):
            raise StateError(f"must be at most {spec['maxLength']} characters")
        if "pattern" in spec and not re.fullmatch(str(spec["pattern"]), value):
            raise StateError("has the wrong format")
        return value
    if kind in ("integer", "number"):
        if isinstance(value, bool):
            raise StateError("must be a number")
        if isinstance(value, str):
            words = {"zero": 0, "none": 0, "no": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                     "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
            value = words.get(value.strip().lower(), value)
        try:
            num = int(value) if kind == "integer" else float(value)
        except (TypeError, ValueError):
            raise StateError("must be a number") from None
        if kind == "integer" and isinstance(value, float) and not float(value).is_integer():
            raise StateError("must be a whole number")
        if "minimum" in spec and num < spec["minimum"]:
            raise StateError(f"must be at least {spec['minimum']}")
        if "maximum" in spec and num > spec["maximum"]:
            raise StateError(f"must be at most {spec['maximum']}")
        if "enum" in spec and num not in spec["enum"]:
            raise StateError("is not an allowed value")
        return num
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("yes", "true", "y"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("no", "false", "n"):
            return False
        raise StateError("must be yes or no")
    if kind == "array":
        if not isinstance(value, list) or len(value) > 50:
            raise StateError("must be a list of at most 50 items")
        item = spec.get("items", {"type": "string", "maxLength": 200})
        return [coerce(name, item, x, today=today) for x in value]
    if kind == "object":
        if not isinstance(value, dict):
            raise StateError("must be an object")
        props = schema_fields(spec)
        out = {}
        for k, x in value.items():
            if k not in props:
                raise StateError(f"has no field {k}")
            out[k] = coerce(f"{name}.{k}", props[k], x, today=today)
        return out
    raise StateError("has an unsupported type")


def empty_state(schema: dict) -> dict:
    data: dict = {name: None for name in schema_fields(schema)}
    for name, value in (("qualification_status", "unknown"), ("disposition", "in_progress"),
                        ("transfer_status", "none")):
        if name in data:
            data[name] = value
    return data


def apply_fields(schema: dict, current: dict, fields: object, *, today: dt.date | None = None,
                 allow_system: bool = True) -> tuple[dict, list[str], dict[str, str]]:
    """Merge ``fields`` into ``current``. Returns (new_state, changed_names, rejected{name: reason}).

    Unknown fields and invalid values are rejected individually; valid ones
    are applied. ``None`` or "" clears a field.
    """
    if not isinstance(fields, dict):
        raise StateError("fields must be an object of field names to values")
    if len(fields) > 40:
        raise StateError("at most 40 fields per update")
    props = schema_fields(schema)
    new = copy.deepcopy(current)
    changed: list[str] = []
    rejected: dict[str, str] = {}
    for name, value in fields.items():
        if not isinstance(name, str) or name not in props:
            rejected[str(name)[:40]] = "is not a field of this intake"
            continue
        if not allow_system and name in SYSTEM_FIELDS and name == "transfer_status":
            rejected[name] = "is managed by the transfer flow"
            continue
        try:
            norm = None if value in (None, "") else coerce(name, props[name], value, today=today)
        except StateError as exc:
            rejected[name] = str(exc)
            continue
        if new.get(name) != norm:
            new[name] = norm
            changed.append(name)
    return new, changed, rejected


def completion(state: dict, required: list[str]) -> dict:
    missing = [f for f in required if state.get(f) in (None, "", [])]
    done = len(required) - len(missing)
    return {"required": list(required), "missing": missing, "filled": done,
            "ratio": round(done / len(required), 3) if required else 1.0}


def speakable(state: dict, changed: list[str], rejected: dict[str, str], missing: list[str]) -> str:
    """A short ASCII sentence for the model (the model card requires ASCII tool responses)."""
    parts = []
    if changed:
        parts.append("Saved " + ", ".join(n.replace("_", " ") for n in changed) + ".")
    for name, reason in list(rejected.items())[:4]:
        parts.append(f"Not saved: {name.replace('_', ' ')} {reason}.")
    if missing:
        parts.append("Still needed: " + ", ".join(m.replace("_", " ") for m in missing[:6]) + ".")
    else:
        parts.append("All required intake fields are complete.")
    return " ".join(parts)[:900]


# ------------------------------------------------------ state reference --
#: General reference for motor-vehicle injury claims by US state, compiled
#: 2026-09 from public statutes. NOT legal advice: the tool says so and the
#: agent must route legal questions to an attorney.
#: sol = general personal-injury statute of limitations in years;
#: fault = negligence rule; insurance = auto insurance system.
STATE_RULES: dict[str, dict] = {
    "AL": {"sol": 2, "fault": "pure contributory negligence", "insurance": "at-fault"},
    "AK": {"sol": 2, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "AZ": {"sol": 2, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "AR": {"sol": 3, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "CA": {"sol": 2, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "CO": {"sol": 3, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault",
           "note": "three years applies to motor vehicle accidents; most other injuries have two"},
    "CT": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "DE": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "DC": {"sol": 3, "fault": "pure contributory negligence", "insurance": "choice no-fault"},
    "FL": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "no-fault",
           "note": "two years for negligence claims arising after March 24, 2023; four years before"},
    "GA": {"sol": 2, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "HI": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "no-fault"},
    "ID": {"sol": 2, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "IL": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "IN": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "IA": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "KS": {"sol": 2, "fault": "modified comparative negligence (50 percent bar)", "insurance": "no-fault"},
    "KY": {"sol": 2, "fault": "pure comparative negligence", "insurance": "choice no-fault",
           "note": "two years for motor vehicle claims under the no-fault act; one year for most other injuries"},
    "LA": {"sol": 2, "fault": "pure comparative fault", "insurance": "at-fault",
           "note": "two years for injuries on or after July 1, 2024; one year before"},
    "ME": {"sol": 6, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "MD": {"sol": 3, "fault": "pure contributory negligence", "insurance": "at-fault"},
    "MA": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "no-fault"},
    "MI": {"sol": 3, "fault": "modified comparative negligence (51 percent bar for non-economic damages)",
           "insurance": "no-fault"},
    "MN": {"sol": 6, "fault": "modified comparative negligence (51 percent bar)", "insurance": "no-fault"},
    "MS": {"sol": 3, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "MO": {"sol": 5, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "MT": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "NE": {"sol": 4, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "NV": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "NH": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "NJ": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "choice no-fault"},
    "NM": {"sol": 3, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "NY": {"sol": 3, "fault": "pure comparative negligence", "insurance": "no-fault"},
    "NC": {"sol": 3, "fault": "pure contributory negligence", "insurance": "at-fault"},
    "ND": {"sol": 6, "fault": "modified comparative negligence (50 percent bar)", "insurance": "no-fault"},
    "OH": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "OK": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "OR": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "PA": {"sol": 2, "fault": "modified comparative negligence (51 percent bar)", "insurance": "choice no-fault"},
    "RI": {"sol": 3, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "SC": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "SD": {"sol": 3, "fault": "slight-gross comparative negligence", "insurance": "at-fault"},
    "TN": {"sol": 1, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "TX": {"sol": 2, "fault": "modified comparative responsibility (51 percent bar)", "insurance": "at-fault"},
    "UT": {"sol": 4, "fault": "modified comparative negligence (50 percent bar)", "insurance": "no-fault"},
    "VT": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "VA": {"sol": 2, "fault": "pure contributory negligence", "insurance": "at-fault"},
    "WA": {"sol": 3, "fault": "pure comparative negligence", "insurance": "at-fault"},
    "WV": {"sol": 2, "fault": "modified comparative negligence (50 percent bar)", "insurance": "at-fault"},
    "WI": {"sol": 3, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
    "WY": {"sol": 4, "fault": "modified comparative negligence (51 percent bar)", "insurance": "at-fault"},
}
STATE_RULES_AS_OF = "2026-09"


def state_rules(state: str, accident_date: str | None = None, today: dt.date | None = None) -> dict:
    code = normalize_state(state or "")
    if code is None:
        raise StateError("unknown US state")
    rule = STATE_RULES[code]
    out = {"state": code, "state_name": US_STATES[code], "statute_of_limitations_years": rule["sol"],
           "negligence_rule": rule["fault"], "insurance_system": rule["insurance"],
           "note": rule.get("note"), "as_of": STATE_RULES_AS_OF,
           "disclaimer": "general reference only, not legal advice; an attorney must confirm deadlines"}
    if accident_date:
        today = today or dt.date.today()
        try:
            d = dt.date.fromisoformat(_date(accident_date, today))
            deadline = d.replace(year=d.year + rule["sol"]) if not (d.month == 2 and d.day == 29) \
                else dt.date(d.year + rule["sol"], 3, 1)
            out["estimated_deadline"] = deadline.isoformat()
            out["days_remaining"] = (deadline - today).days
        except StateError:
            pass
    return out


def state_rules_sentence(info: dict) -> str:
    text = (f"{info['state_name']}: general injury filing deadline is about {info['statute_of_limitations_years']} "
            f"years; negligence rule is {info['negligence_rule']}; insurance system is {info['insurance_system']}.")
    if info.get("note"):
        text += f" Note: {info['note']}."
    if "days_remaining" in info:
        days = info["days_remaining"]
        text += (f" From the accident date, roughly {days} days remain." if days >= 0
                 else " The general deadline may already have passed; an attorney must review this quickly.")
    return text + " This is general information, not legal advice."


# ------------------------------------------------------- business hours --
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def check_hours(hours: object) -> dict:
    """Validate {timezone, days: {mon: [["09:00","17:00"]], ...}, closed_dates: ["2026-12-25"]}."""
    if hours in (None, {}):
        return {}
    if not isinstance(hours, dict):
        raise StateError("business hours must be an object")
    tz = hours.get("timezone", "America/New_York")
    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError):
        raise StateError(f"unknown timezone {tz!r}") from None
    days = hours.get("days") or {}
    if not isinstance(days, dict) or set(days) - set(DAYS):
        raise StateError("days must use mon..sun keys")
    out_days = {}
    for day, spans in days.items():
        if not isinstance(spans, list) or len(spans) > 4:
            raise StateError(f"{day}: up to 4 [open, close] spans")
        clean = []
        for span in spans:
            if (not isinstance(span, list) or len(span) != 2
                    or not all(isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in span)
                    or span[0] >= span[1]):
                raise StateError(f"{day}: spans look like [\"09:00\", \"17:00\"]")
            clean.append([span[0], span[1]])
        out_days[day] = clean
    closed = hours.get("closed_dates") or []
    if not isinstance(closed, list) or len(closed) > 60:
        raise StateError("closed_dates must be a list of up to 60 dates")
    for d in closed:
        try:
            dt.date.fromisoformat(str(d))
        except ValueError:
            raise StateError(f"invalid closed date {d!r}") from None
    return {"timezone": str(tz), "days": out_days, "closed_dates": [str(d) for d in closed]}


def hours_status(hours: dict, now: dt.datetime | None = None) -> dict:
    if not hours:
        return {"configured": False, "open": True, "detail": "no business hours configured (always open)"}
    tz = ZoneInfo(hours["timezone"])
    now = (now or dt.datetime.now(dt.UTC)).astimezone(tz)

    def open_at(moment: dt.datetime) -> bool:
        if moment.date().isoformat() in hours.get("closed_dates", []):
            return False
        hm = moment.strftime("%H:%M")
        return any(a <= hm < b for a, b in hours["days"].get(DAYS[moment.weekday()], []))

    is_open = open_at(now)
    nxt = None
    if not is_open:
        for offset in range(0, 8):
            day = (now + dt.timedelta(days=offset)).date()
            if day.isoformat() in hours.get("closed_dates", []):
                continue
            for a, _b in sorted(hours["days"].get(DAYS[day.weekday()], [])):
                start = dt.datetime.combine(day, dt.time.fromisoformat(a), tzinfo=tz)
                if start > now:
                    nxt = start
                    break
            if nxt:
                break
    return {"configured": True, "open": is_open, "timezone": hours["timezone"],
            "local_time": now.strftime("%A %H:%M"), "next_open": nxt.isoformat() if nxt else None,
            "next_open_spoken": nxt.strftime("%A at %I:%M %p").replace(" 0", " ") if nxt else None}
