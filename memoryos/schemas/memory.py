from pydantic import BaseModel, Field
from typing import List, Optional

class MemoryIngest(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    content: str = Field(..., description="The factual text to ingest")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    session_id: Optional[str] = Field(None, description="Optional active session ID")

class MemoryRetrieve(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    query: str = Field(..., description="The query to search memories for")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    limit: int = Field(5, ge=1, le=20, description="Max memories to return")
    current_goal: Optional[str] = Field(None, description="Optional active agent goal to align retrieval context")

class IngestResponse(BaseModel):
    status: str
    memory_id: str
    message: str

class MemoryItem(BaseModel):
    memory_id: str
    content: str
    score: float
    type: str
    created_at: str
    confidence: Optional[float] = 0.5
    importance: Optional[float] = 0.5
    frequency: Optional[int] = 1
    recency: Optional[float] = 1.0
    verification: Optional[str] = "unverified"
    source: Optional[str] = "user"
    decay: Optional[float] = 1.0

class RetrieveResponse(BaseModel):
    results: List[MemoryItem]
    context_token_count: int
    goal_category: Optional[str] = None

class MemoryReflect(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    workspace_id: str = Field("default", description="Workspace separation parameter")

class WorkflowIngest(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    name: str = Field(..., description="Name of the workflow")
    description: Optional[str] = Field(None, description="Detailed workflow description")
    steps: List[str] = Field(..., description="Ordered step description strings")

class WorkflowResponse(BaseModel):
    status: str
    workflow_id: str
    message: str

class WorkingMemoryUpdate(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    current_goal: Optional[str] = Field(None, description="Active goal string")
    constraints: Optional[List[str]] = Field(None, description="List of active constraints")
    current_plan: Optional[List[str]] = Field(None, description="List of plan items")
    scratchpad: Optional[str] = Field(None, description="General notes/thoughts")
    retained_facts: Optional[List[str]] = Field(None, description="Retained short-term facts")

class WorkingMemoryResponse(BaseModel):
    user_id: str
    workspace_id: str
    current_goal: Optional[str]
    constraints: List[str]
    current_plan: List[str]
    scratchpad: str
    retained_facts: List[str]
