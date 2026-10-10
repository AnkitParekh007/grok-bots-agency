"""Skill registry. Generic agent capabilities live in runtime.py; domain knowledge lives in skills.
A skill exposes named actions; the model may only reference registered skill/action pairs, and
actions only touch the target through the ToolContext (i.e. via the gateway)."""
from ..interfaces import SkillRegistry
from . import pim_import_testing, attribute_mapping_validation


class Skill:
    def __init__(self, module):
        self.name = module.NAME
        self.description = module.DESCRIPTION
        self.version = module.VERSION
        self.actions = module.ACTIONS  # name -> (type, fn)
        self.params = getattr(module, "PARAMS", {})


class LocalSkillRegistry(SkillRegistry):
    def __init__(self, modules=(pim_import_testing, attribute_mapping_validation)):
        self.skills = {m.NAME: Skill(m) for m in modules}

    def get(self, name):
        if name not in self.skills:
            raise KeyError(f"skill {name!r} not registered")
        return self.skills[name]

    def list(self):
        return [{"name": s.name, "version": s.version, "description": s.description,
                 "actions": {a: t for a, (t, _) in s.actions.items()}, "params": s.params} for s in self.skills.values()]
