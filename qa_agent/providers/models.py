import os
from pathlib import Path
from ..interfaces import ModelProvider
from ..util import load_json


class ProviderNotApproved(Exception):
    pass


class ReplayModelProvider(ModelProvider):
    name = "replay"

    def __init__(self, replay_dir: Path, label="REPLAYED (mock model)"):
        self.dir, self.label = Path(replay_dir), label

    def complete(self, task, context):
        key = context.get("story_key")
        p = self.dir / f"{key}.json"
        if not p.exists():
            raise KeyError(f"no recording for {key}")
        rec = load_json(p)
        if task not in rec.get("responses", {}):
            raise KeyError(f"no recorded response for task {task}")
        return rec["responses"][task]


class GrokProvider(ModelProvider):
    """xAI/Grok via OpenAI-compatible API. DISABLED in Milestone A.
    Fails closed unless config enabled=true AND approved=true AND approval_ref set AND key present
    AND the request data class is in allowed_data_classes. Even then, the live call is not
    implemented until Milestone B (no network code ships here)."""
    name = "grok"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.label = f"LIVE ({cfg.get('model')})"
        missing = [k for k in ("enabled", "approved") if cfg.get(k) is not True]
        if missing or not cfg.get("approval_ref"):
            raise ProviderNotApproved(f"grok provider not enabled/approved (missing: {missing or ['approval_ref']})")
        if not os.environ.get(cfg.get("api_key_env", "XAI_API_KEY")):
            raise ProviderNotApproved("grok api key not provided via approved secret reference")

    def complete(self, task, context):
        if context.get("data_class") not in self.cfg.get("allowed_data_classes", []):
            raise ProviderNotApproved("data class not approved for this provider")
        raise NotImplementedError("Live Grok calls are Milestone B work")


def build_model_provider(agent_cfg: dict, root: Path) -> ModelProvider:
    name = os.environ.get("QA_MODEL_PROVIDER") or agent_cfg["model"]["provider"]
    pc = agent_cfg["providers"].get(name)
    if pc is None:
        raise ProviderNotApproved(f"unknown model provider {name!r}")
    if name == "replay":
        return ReplayModelProvider(root / pc["replay_dir"], pc.get("label", "REPLAYED (mock model)"))
    if name == "grok":
        return GrokProvider(pc)
    raise ProviderNotApproved(f"provider {name!r} has no implementation")
