import time
from typing import List, Optional

class WorkingMemoryRegister:
    """
    A structured CPU-style Working Memory Register to manage:
    - current_goal: Active agent goal string.
    - constraints: Specific guardrails/constraints (e.g. tech stack, budget limits).
    - current_plan: High-level task list/steps.
    - scratchpad: A text buffer for general agent notes/thought process.
    - retained_facts: Short-term facts pulled out of long-term RAG storage.
    """
    def __init__(self):
        self._registers = {}  # key: (user_id, workspace_id) -> dict
        
    def get_register(self, user_id: str, workspace_id: str = "default") -> dict:
        key = (user_id, workspace_id)
        if key not in self._registers:
            self._registers[key] = {
                "current_goal": None,
                "constraints": [],
                "current_plan": [],
                "scratchpad": "",
                "retained_facts": [],
                "last_updated": time.time()
            }
        return self._registers[key]
        
    def update_register(self, user_id: str, workspace_id: str = "default", **kwargs) -> dict:
        reg = self.get_register(user_id, workspace_id)
        for k, v in kwargs.items():
            if k in reg and v is not None:
                reg[k] = v
        reg["last_updated"] = time.time()
        return reg
        
    def clear_register(self, user_id: str, workspace_id: str = "default"):
        key = (user_id, workspace_id)
        if key in self._registers:
            self._registers[key] = {
                "current_goal": None,
                "constraints": [],
                "current_plan": [],
                "scratchpad": "",
                "retained_facts": [],
                "last_updated": time.time()
            }

working_memory = WorkingMemoryRegister()
