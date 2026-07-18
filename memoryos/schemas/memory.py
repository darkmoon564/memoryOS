import re
from datetime import datetime, timezone
from pydantic import BaseModel, Field, validator
from typing import List, Optional

def validate_id_string(v: str) -> str:
    if v is not None:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError("ID must contain only alphanumeric characters, underscores, and hyphens (no spaces, slashes, or special characters).")
    return v

class MemoryIngest(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    content: str = Field(..., description="The factual text to ingest")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    session_id: Optional[str] = Field(None, description="Optional active session ID")
    occurred_at: Optional[datetime] = Field(None, description="Timezone-aware time when this event occurred")
    source_event_id: Optional[str] = Field(None, description="Optional stable identifier of the originating turn or event")

    @validator("user_id", "workspace_id")
    def check_ids(cls, v):
        return validate_id_string(v)

    @validator("content")
    def check_content_length(cls, v):
        if not v or not v.strip():
            raise ValueError("Memory content must not be empty.")
        if len(v) > 10000:
            raise ValueError("Memory content size exceeds maximum limit of 10000 characters.")
        return v.strip()

    @validator("occurred_at")
    def check_occurred_at(cls, v):
        if v is not None and v.tzinfo is None:
            raise ValueError("occurred_at must include a timezone offset.")
        return v.astimezone(timezone.utc) if v is not None else v

    @validator("source_event_id")
    def check_source_event_id(cls, v):
        if v is None:
            return v
        value = v.strip()
        if not value:
            raise ValueError("source_event_id must not be blank.")
        if len(value) > 256 or "\x00" in value:
            raise ValueError("source_event_id must be at most 256 characters and contain no null bytes.")
        return value

class MemoryRetrieve(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    query: str = Field(..., description="The query to search memories for")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    limit: int = Field(5, ge=1, le=20, description="Max memories to return")
    current_goal: Optional[str] = Field(None, description="Optional active agent goal to align retrieval context")

    @validator("user_id", "workspace_id")
    def check_ids(cls, v):
        return validate_id_string(v)

    @validator("query")
    def check_query_length(cls, v):
        if not v or not v.strip():
            raise ValueError("Query must not be empty.")
        if len(v) > 1000:
            raise ValueError("Query size exceeds maximum limit of 1000 characters.")
        return v.strip()

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
    occurred_at: Optional[str] = None
    source_event_ids: List[str] = Field(default_factory=list)
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

    @validator("user_id", "workspace_id")
    def check_ids(cls, v):
        return validate_id_string(v)

class WorkflowIngest(BaseModel):
    user_id: str = Field(..., description="ID of the user")
    workspace_id: str = Field("default", description="Workspace separation parameter")
    name: str = Field(..., description="Name of the workflow")
    description: Optional[str] = Field(None, description="Detailed workflow description")
    steps: List[str] = Field(..., description="Ordered step description strings")

    @validator("user_id", "workspace_id")
    def check_ids(cls, v):
        return validate_id_string(v)

    @validator("name")
    def check_name_length(cls, v):
        if len(v) > 256:
            raise ValueError("Workflow name exceeds maximum limit of 256 characters.")
        return v

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

    @validator("user_id", "workspace_id")
    def check_ids(cls, v):
        return validate_id_string(v)

class WorkingMemoryResponse(BaseModel):
    user_id: str
    workspace_id: str
    current_goal: Optional[str]
    constraints: List[str]
    current_plan: List[str]
    scratchpad: str
    retained_facts: List[str]
