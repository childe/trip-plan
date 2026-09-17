用户对行程给出了反馈。判断它属于哪一类：

- **改行程**（patches_requirements = false）：需求没变，只是这份行程排得不好。
  例："第2天太赶了"、"不想去这个博物馆"。
- **改需求**（patches_requirements = true）：需求本身变了。
  例："三天改四天"、"预算加到2万"、"改去日本"。

判为改需求时，在 patch 里给出要修改的字段与新值，并判断 scale：
- `INCREMENTAL`：现有行程仍可作为起点（加一天、调预算）。
- `REWRITE`：现有行程整体作废（换目的地）。

**patch 的键只能是下面这些**，其余一律会被丢弃，不要自创字段名：

| 键 | 值的形状 | 例 |
|---|---|---|
| `destination` | 字符串 | `"芜湖"` |
| `dates` | `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}` | `{"start": "2026-09-26", "end": "2026-09-27"}` |
| `party` | `{"adults": N, "children": N, "seniors": N}` | `{"adults": 2}` |
| `arrival` / `departure` | `{"at": "YYYY-MM-DDTHH:MM", "mode": "..."}` | `{"at": "2026-09-26T09:00", "mode": "高铁"}` |
| `budget` | `{"amount": N, "currency": "CNY", "basis": "TOTAL\|PER_PERSON", "includes": [...]}` | |
| `pace` | `"RELAXED"` / `"NORMAL"` / `"PACKED"` | |
| `styles` / `must_visit` / `avoid` / `constraints` | 字符串数组 | `["骑行", "咖啡"]` |
| `lodging_area` | 字符串 | |

注意 `dates` 只有 `start` 和 `end` 两个键。**不要**写成 `start_date` /
`end_date` / `duration_days`——那样整条 patch 会被丢掉，用户的话等于没说。
天数由 start 与 end 推出来，不单独给。

patch 是**部分更新**：只放真正要改的键，没提到的字段保持原样。

只输出 JSON。
