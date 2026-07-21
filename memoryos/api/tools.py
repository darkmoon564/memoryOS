from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Header, HTTPException, Query
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.api.memories import ingest_memory, retrieve_context, verify_workspace_key
from memoryos.core.episodes import query_llm
from memoryos.services.llm_usage import get_daily_usage

router = APIRouter()


@router.get("/v1/ceo/usage")
async def get_ceo_llm_usage(
    workspace_id: str = Query(...),
    authorization: Optional[str] = Header(None),
):
    verify_workspace_key(workspace_id, authorization)
    return get_daily_usage()


@router.post("/v1/ceo/ask")
async def ask_ceo_agent(
    arguments: dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
):
    """Answer an executive question using only tenant-scoped MemoryOS context."""
    user_id = arguments.get("user_id")
    workspace_id = arguments.get("workspace_id")
    question = arguments.get("query")
    if not user_id or not workspace_id or not question:
        raise HTTPException(status_code=400, detail="user_id, workspace_id, and query are required.")

    retrieval_data = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query=question,
        limit=min(int(arguments.get("limit", 6)), 10),
    )
    retrieved = await retrieve_context(retrieval_data, authorization=authorization)
    memories = [item.dict() for item in retrieved.results]
    if not memories:
        return {"answer": "I could not find relevant company context in MemoryOS yet.", "results": []}

    context = "\n".join(
        f"[{index}] {item['content']}"
        for index, item in enumerate(memories, start=1)
    )
    system_prompt = (
        "You are a clear, pragmatic startup CEO copilot. Answer only from the supplied MemoryOS context. "
        "Do not invent facts, metrics, dates, or owners. State uncertainty where context is incomplete. "
        "Be concise: the first sentence must answer the CEO's exact question directly and explicitly. "
        "For example, if asked which customer needs attention, name the customer first, then explain why. "
        "Follow with 2-4 short supporting points and a practical next action. "
        "Use plain text only: no Markdown asterisks, headings, or tables."
    )
    answer = query_llm(
        system_prompt,
        f"CEO question: {question}\n\nRelevant MemoryOS context:\n{context}",
    )
    if not answer:
        answer = "Relevant company context:\n" + "\n".join(f"- {item['content']}" for item in memories[:3])
    return {"answer": answer, "results": memories}

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
