"""按同一任务和目标语言保留有证据的人名与身份。"""
from __future__ import annotations

import unicodedata

_ZH_TITLES = {
    "research director": "研究总监", "program guest": "节目嘉宾", "guest": "嘉宾",
    "host": "主持人", "co-host": "联合主持人", "founder": "创始人", "co-founder": "联合创始人",
    "prof": "教授", "professor": "教授", "dr": "博士", "researcher": "研究员",
    "director": "总监", "reporter": "记者", "journalist": "记者", "editor": "编辑",
    "producer": "制片人", "chief executive officer": "首席执行官",
    "ceo": "首席执行官", "cto": "首席技术官", "engineer": "工程师", "coach": "教练",
}


def person_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace(".", "").replace("’", "'").split())


def localized_title(value: str, target: str) -> str:
    return _ZH_TITLES.get(person_key(value), value) if target.casefold().split("-")[0] == "zh" else value


class PersonRegistry:
    def __init__(self, records):
        self._people = {}
        for record in records:
            if not isinstance(record, dict) or record.get("kind") != "person":
                continue
            source, target = record.get("source"), record.get("target")
            if isinstance(source, str) and source and isinstance(target, str) and target:
                self._people.setdefault(person_key(source), []).append(record)

    @staticmethod
    def _consensus(records, field):
        values = {value for record in records if isinstance(value := record.get(field), str) and value}
        return next(iter(values)) if len(values) == 1 else ""

    def resolve(self, source: str, at: float | None, target: str = "") -> tuple[str, str]:
        records = self._people.get(person_key(source), [])
        if target:
            records = [record for record in records if person_key(record["target"]) == person_key(target)]
        nearby = [record for record in records if isinstance(at, (int, float))
                  and isinstance(start := record.get("start", record.get("id")), (int, float))
                  and abs(start - at) <= 4]
        selected = nearby or records
        name = self._consensus(selected, "target")
        if not name:
            return "", ""
        title = self._consensus(selected, "sense")
        if not title and not any(record.get("sense") for record in selected):
            title = self._consensus([record for record in records if record["target"] == name], "sense")
        return name, title


def inherit_confirmed_titles(mentions: list[dict]) -> list[dict]:
    registry = PersonRegistry(mentions)
    result = []
    for record in mentions:
        confirmed = registry.resolve(record["source"], record.get("id"), record["target"])[1]
        row = dict(record)
        proposed = row.pop("_proposed_sense", "")
        if not row["sense"] and (not proposed or person_key(proposed) == person_key(confirmed)):
            row["sense"] = confirmed
        result.append(row)
    return result
