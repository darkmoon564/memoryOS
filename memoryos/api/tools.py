from fastapi import APIRouter, Query, HTTPException, BackgroundTasks
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.services.background import background_graph_ingest
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
                        "query": {"type": "string", "description": "Search query text"},
                        "limit": {"type": "integer", "default": 5}
                    },
                    "required": ["user_id", "query"]
                }
            },
            {
                "name": "create_memory",
                "description": "Ingest a new factual assertion, episodic log, or preference into the long-term store.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "User identifier"},
                        "content": {"type": "string", "description": "Fact text to write"}
                    },
                    "required": ["user_id", "content"]
                }
            }
        ]
    }

@router.post("/tools/call")
async def mcp_call_tool(
    tool_name: str = Query(..., alias="name"),
    arguments: dict = {},
    bg_tasks: BackgroundTasks = BackgroundTasks()
):
    """Executes MCP tool routing calls directly."""
    user_id = arguments.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id parameter.")
        
    if tool_name == "get_memories":
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not query:
            raise HTTPException(status_code=400, detail="Missing query parameter.")
        retrieval_data = MemoryRetrieve(user_id=user_id, query=query, limit=limit)
        response = await retrieve_context(retrieval_data)
        formatted_text = "\n".join([f"- {r.content}" for r in response.results])
        return {"content": [{"type": "text", "text": formatted_text}]}
        
    elif tool_name == "create_memory":
        content = arguments.get("content")
        if not content:
            raise HTTPException(status_code=400, detail="Missing content parameter.")
        ingest_data = MemoryIngest(user_id=user_id, content=content)
        response = await ingest_memory(ingest_data, bg_tasks)
        # Execute background graph tasks synchronously for immediate tool response validation
        background_graph_ingest(response.memory_id, content, user_id, "default")
        return {"content": [{"type": "text", "text": f"Successfully ingested memory ID: {response.memory_id}"}]}

    raise HTTPException(status_code=404, detail="Tool not found")
