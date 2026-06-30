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

class RetrieveResponse(BaseModel):
    results: List[MemoryItem]
    context_token_count: int
