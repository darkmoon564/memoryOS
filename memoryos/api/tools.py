from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.api.memories import ingest_memory, retrieve_context

router = APIRouter()

@router.get("/tools")
async def mcp_list_tools():
    """Lists MCP tools available to LLM agent interfaces."""
    return {
        "tools": [
            {
                "name": "get_memories",
                "description": "Fetch high-context historical user memory records using semantic search.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "User identifier"},
                        "workspace_id": {"type": "string", "description": "Authorized workspace identifier", "default": "default"},
                        "query": {"type": "string", "description": "Search query text"},
                        "limit": {"type": "integer", "default": 5}
                    },
                    "required": ["user_id", "query", "workspace_id"]
                }
            },
            {
                "name": "create_memory",
                "description": "Ingest a new factual assertion, episodic log, or preference into the long-term store.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "User identifier"},
                        "workspace_id": {"type": "string", "description": "Authorized workspace identifier", "default": "default"},
                        "content": {"type": "string", "description": "Fact text to write"}
                    },
                    "required": ["user_id", "content", "workspace_id"]
                }
            }
        ]
    }

@router.post("/tools/call")
async def mcp_call_tool(
    tool_name: str = Query(..., alias="name"),
    arguments: dict[str, Any] = Body(...),
    bg_tasks: BackgroundTasks = None,
    authorization: Optional[str] = Header(None)
):
    """Execute tool calls with the same tenant authorization as the REST API."""
    user_id = arguments.get("user_id")
    workspace_id = arguments.get("workspace_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id parameter.")
    if not workspace_id:
        raise HTTPException(status_code=400, detail="Missing workspace_id parameter.")
        
    if tool_name == "get_memories":
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not query:
            raise HTTPException(status_code=400, detail="Missing query parameter.")
        retrieval_data = MemoryRetrieve(user_id=user_id, workspace_id=workspace_id, query=query, limit=limit)
        response = await retrieve_context(retrieval_data, authorization=authorization)
        formatted_text = "\n".join([f"- {r.content}" for r in response.results])
        return {"content": [{"type": "text", "text": formatted_text}]}
        
    elif tool_name == "create_memory":
        content = arguments.get("content")
        if not content:
            raise HTTPException(status_code=400, detail="Missing content parameter.")
        ingest_data = MemoryIngest(user_id=user_id, workspace_id=workspace_id, content=content)
        response = await ingest_memory(ingest_data, bg_tasks, authorization=authorization)
        return {"content": [{"type": "text", "text": f"Successfully ingested memory ID: {response.memory_id}"}]}

    raise HTTPException(status_code=404, detail="Tool not found")
