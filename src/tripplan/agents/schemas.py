"""各角色的输出 schema。

run_agent 校验顶层 required，以及**递归地**校验这里声明的每一个 type
（见 runner._check_types）。更细的语义校验——枚举值认不认识、日期能不能
解析、嵌套层缺了哪个键——仍然交给 steps.py 的转换函数：那里有「跳过这一
项、留一条 WARNING」的部分成功语义，报错信息也更具体。

所以这个文件里的 type 声明不是文档，是有执行力的合同：写 `"type": "string"`
就意味着模型给 null 会被打回重来。哪里允许 null，要按下游是否能把 null
当成「没给」优雅降级来定，不能随手加宽。
"""

_FIELD = {
    # 允许 null：模型用 "budget": null 表达「这一项没给」是合理的，_to_field
    # 对非 dict 一律降级成空 Field，不会有任何毒数据流下去。但**不允许**把
    # 信封拍平成 "destination": "京都"——那会让 _to_field 把整个字段当没给，
    # 用户明明说了的信息被静默丢光（评审 I5/I8），必须打回让模型重写。
    "type": ["object", "null"],
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
                                # cost 必须往里声明一层：_to_activity 直接把
                                # currency 塞进 Money，null 会一路活到
                                # escape(cost.currency) 才炸，且 state.json
                                # 能干净往返，`trip render` 会永远复现。
                                "cost": {
                                    "type": ["object", "null"],
                                    "properties": {
                                        # 模型两种写法都给过（400 与 "400"），
                                        # Decimal(str(...)) 两种都吃；真正
                                        # 非数字的字符串留给 steps 降级。
                                        "amount": {"type": ["number", "string"]},
                                        "currency": {"type": "string"},
                                    },
                                },
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
        # 允许 null：「这次反馈不改需求」时模型写 "patch": null 很自然，
        # classify_feedback 的 `or {}` 本来就把它当空补丁，没有任何损失。
        "patch": {"type": ["object", "null"]},
        "scale": {"type": "string"},
    },
    "required": ["patches_requirements"],
}
