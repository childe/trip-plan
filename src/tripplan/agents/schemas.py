"""各角色的输出 schema。run_agent 只校验 required 与顶层类型，
更细的语义校验交给 steps.py 的转换函数——那里报错信息更具体。"""

_FIELD = {
    "type": "object",
    "properties": {
        "value": {},
        "origin": {"type": ["string", "null"]},
        "rationale": {"type": "string"},
    },
    "required": ["value"],
}

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        name: _FIELD
        for name in (
            "destination",
            "dates",
            "party",
            "arrival",
            "departure",
            "budget",
            "styles",
            "pace",
            "must_visit",
            "avoid",
            "lodging_area",
            "constraints",
        )
    },
    "required": ["destination", "dates", "party"],
}

ANGLES_SCHEMA = {
    "type": "object",
    "properties": {
        "angles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["key", "title"],
            },
        }
    },
    "required": ["angles"],
}

ITINERARY_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "lodging": {"type": ["string", "null"]},
                    "activities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "poi_query": {"type": "string"},
                                "start": {"type": "string"},
                                "end": {"type": "string"},
                                "category": {"type": "string"},
                                "cost": {"type": ["object", "null"]},
                                "indoor": {"type": "boolean"},
                                "note": {"type": "string"},
                            },
                            "required": ["poi_query", "start", "end", "category"],
                        },
                    },
                },
                "required": ["date", "activities"],
            },
        }
    },
    "required": ["days"],
}

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                    "where_day": {"type": ["string", "null"]},
                },
                "required": ["severity", "message"],
            },
        }
    },
    "required": ["issues"],
}

FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "patches_requirements": {"type": "boolean"},
        "patch": {"type": "object"},
        "scale": {"type": "string"},
    },
    "required": ["patches_requirements"],
}
