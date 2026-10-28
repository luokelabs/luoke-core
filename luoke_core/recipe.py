"""配方（V1 §4）：主控据以工作的一整份数据。开发版从一个 JSON 文件读，格式见 recipes/dev.json 和下面的校验。

硬件档只按统一内存分；每档一个底座、若干专用模型；任务表把任务名（<配件标识>.<任务>）对到配件、调用方式、
任务参数（与内存有关的按档细分）。所有以 token 或 nats 为单位的常数都在任务参数里，配件只读。
"""
from __future__ import annotations

import json
import re
from typing import Optional

TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9.-]*$")


class RecipeError(ValueError):
    pass


class Recipe:
    def __init__(self, data: dict):
        self.data = data
        self.version = str(data.get("recipe_version", "?"))
        if data.get("contract") != 1:
            raise RecipeError("配方的 contract 不是 1")
        self.tiers: dict = data["tiers"]
        self.models: dict = data["models"]
        self.assignment: dict = data["assignment"]
        self.tasks: dict = data["tasks"]
        self.engine: dict = data["engine"]
        self.verified: dict = data.get("verified", {})
        self._check()

    @classmethod
    def load(cls, path: str) -> "Recipe":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def _check(self) -> None:
        for name, t in self.tiers.items():
            for k in ("min_gb", "max_gb", "guard_gb", "base_resident", "background_input_chars"):
                if k not in t:
                    raise RecipeError(f"硬件档 {name} 缺 {k}")
        for tier, a in self.assignment.items():
            if tier not in self.tiers:
                raise RecipeError(f"模型分配里的档 {tier} 没有定义")
            if a.get("base") is not None and a["base"] not in self.models:
                raise RecipeError(f"档 {tier} 的底座 {a['base']} 不在模型清单里")
            for task, m in (a.get("special") or {}).items():
                if m not in self.models:
                    raise RecipeError(f"档 {tier} 任务 {task} 的专用模型 {m} 不在模型清单里")
        for name, t in self.tasks.items():
            if not TASK_NAME.match(name):
                raise RecipeError(f"任务名不合写法：{name}")
            if t.get("kind") not in ("generate", "score", "predict", "embed"):
                raise RecipeError(f"任务 {name} 的 kind 要是 generate、score、predict 或 embed")
            if not t.get("accessory") or not name.startswith(t["accessory"] + "."):
                raise RecipeError(f"任务 {name} 的 accessory 和任务名前缀不一致")
            if "params" not in t:
                raise RecipeError(f"任务 {name} 没有任务参数")
        for name, m in self.models.items():
            for k in ("dir", "kind", "wired_gb"):
                if k not in m:
                    raise RecipeError(f"模型 {name} 缺 {k}")
        for tier, a in self.assignment.items():      # 嵌入任务用的模型必须声明向量空间（配件靠它判断旧向量还能不能接着用）
            for task, m in (a.get("special") or {}).items():
                if (self.tasks.get(task) or {}).get("kind") == "embed" and not isinstance(self.models[m].get("vector_space"), str):
                    raise RecipeError(f"档 {tier} 嵌入任务 {task} 的模型 {m} 没有 vector_space")

    # ---- 查询 --------------------------------------------------------------------------------------
    def tier_for(self, mem_gb: float) -> Optional[str]:
        for name, t in self.tiers.items():
            if t["min_gb"] <= mem_gb < t["max_gb"]:
                return name
        return None

    def tier(self, name: str) -> dict:
        return self.tiers[name]

    def base_for(self, tier: str) -> Optional[str]:
        return (self.assignment.get(tier) or {}).get("base")

    def special_for(self, tier: str, task: str) -> Optional[str]:
        return ((self.assignment.get(tier) or {}).get("special") or {}).get(task)

    def model(self, name: str) -> dict:
        return self.models[name]

    def task(self, name: str) -> Optional[dict]:
        return self.tasks.get(name)

    def tasks_for(self, accessory: str) -> dict:
        return {n: t for n, t in self.tasks.items() if t["accessory"] == accessory}

    def params(self, task: str, tier: str) -> dict:
        """任务参数：先取按档细分的，再用通用的补齐。"""
        p = self.tasks[task]["params"]
        out = dict(p.get("*", {}))
        out.update(p.get(tier, {}))
        return out

    def base_params(self, task: str) -> dict:
        return dict(self.tasks[task].get("base_params", {}))

    def model_for_task(self, task: str, tier: str) -> Optional[str]:
        t = self.tasks[task]
        if t["kind"] in ("score", "predict", "embed"):
            return self.special_for(tier, task)
        return self.base_for(tier)

    def vector_space(self, task: str, tier: str) -> Optional[str]:
        """嵌入任务在这一档的向量空间标识（例如 "embeddinggemma-2/1"）；不是嵌入任务或这一档没分配模型就 None。
        实验室定：换了模型或引擎以后，新向量对参考向量集仍然余弦 ≥ 0.999 就不变，否则后面的数加一。配件只比相等，不解析。"""
        t = self.tasks.get(task)
        if not t or t["kind"] != "embed":
            return None
        m = self.model_for_task(task, tier)
        return self.models[m].get("vector_space") if m else None

    def speed(self, model: str, tier: str, key: str, default: float) -> float:
        v = self.models[model].get(key)
        if isinstance(v, dict):
            return float(v.get(tier, v.get("*", default)))
        return float(v) if v is not None else default

    def version_verified(self, accessory: str, version: str) -> bool:
        v = self.verified.get(accessory)
        if not v:
            return False
        allowed = v.get("versions", [])
        return "*" in allowed or version in allowed
