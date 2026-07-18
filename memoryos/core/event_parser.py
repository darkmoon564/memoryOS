import os
import json
import requests
from memoryos.config import logger
from memoryos.services.extractor import load_spacy_model

# ──────────────────────────────────────────────────────────────────────
# LLM Splitting Prompt
# ──────────────────────────────────────────────────────────────────────

EVENT_PARSER_SYSTEM_PROMPT = (
    "You are an expert sentence splitting assistant. Your task is to break down compound or complex sentences "
    "into a flat list of independent, atomic, self-contained factual or episodic statements.\n"
    "Rules:\n"
    "1. Resolve pronouns (e.g. change 'she', 'he', 'they' to their referring name if clear from context).\n"
    "2. Keep the statements short, concise, and focused on a single fact or event.\n"
    "3. Maintain original meaning, timeline, and qualifiers.\n"
    "4. Return the output STRICTLY in JSON format matching this schema: {\"events\": [\"statement 1\", \"statement 2\"]}.\n"
    "Do NOT wrap the output in markdown code blocks."
)

# ──────────────────────────────────────────────────────────────────────
# LLM API Splitting
# ──────────────────────────────────────────────────────────────────────

def _extract_events_via_llm_api(content: str) -> list | None:
    """Attempts to split content into atomic events using a configured LLM API."""
    if os.getenv("OFFLINE_MODE", "false").lower() == "true":
        # Offline mode is a hard no-network contract for deterministic tests
        # and air-gapped deployments; proceed directly to the local parser.
        return None
    timeout = float(os.getenv("LLM_TIMEOUT", "15.0"))
    
    # ── Mode 1: OpenAI-compatible API ──
    llm_api_base = os.getenv("LLM_API_BASE")
    llm_api_key = os.getenv("LLM_API_KEY")
    llm_model = os.getenv("LLM_MODEL")
    
    if llm_api_base and llm_api_key and llm_model:
        try:
            url = f"{llm_api_base.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {llm_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": EVENT_PARSER_SYSTEM_PROMPT},
                    {"role": "user", "content": content}
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"}
            }
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code == 200:
                raw_text = response.json()["choices"][0]["message"]["content"]
                result = json.loads(raw_text)
                logger.info(f"[EventParser] Sentences split completed via LLM API ({llm_model})")
                return result.get("events", [])
            else:
                logger.warning(f"LLM API returned status {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"LLM API sentence splitting failed: {e}")
        return None
    
    # ── Mode 2: Ollama (local LLM) ──
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2")
    
    try:
        response = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": EVENT_PARSER_SYSTEM_PROMPT},
                    {"role": "user", "content": content}
                ],
                "format": "json",
                "stream": False
            },
            timeout=timeout
        )
        if response.status_code == 200:
            raw_text = response.json().get("message", {}).get("content", "")
            result = json.loads(raw_text)
            logger.info(f"[EventParser] Sentences split completed via Ollama ({ollama_model})")
            return result.get("events", [])
    except Exception as e:
        logger.warning(f"Ollama sentence splitting failed: {e}. Using spaCy-based fallback parser.")
    
    return None

# ──────────────────────────────────────────────────────────────────────
# spaCy Grammatical Clause Splitter (Fallback)
# ──────────────────────────────────────────────────────────────────────

def _split_conjunction_clauses(sent) -> list:
    """
    Splits compound coordinate clauses linked by conjunctions using dependency grammar.
    E.g. 'Alice works at Acme and loves coding in Rust' ->
         ['Alice works at Acme.', 'Alice loves coding in Rust.']
    """
    if len(sent) < 5:
        return [sent.text.strip()]
        
    conj_verbs = []
    root_verb = None
    subject_tokens = []
    
    for token in sent:
        if token.dep_ == "ROOT":
            root_verb = token
        elif token.dep_ == "conj" and token.pos_ in ["VERB", "AUX"]:
            conj_verbs.append(token)
        # Find subject of the ROOT verb
        if token.dep_ in ["nsubj", "nsubjpass"] and token.head.dep_ == "ROOT":
            subject_tokens.append(token)
            
    if not conj_verbs or not root_verb or not subject_tokens:
        return [sent.text.strip()]
        
    # Reconstruct subject phrase
    subj = subject_tokens[0]
    subject_text = " ".join([t.text for t in subj.subtree]).strip()
    
    # Exclude conj verb subtrees and their conjunction connectors from ROOT clause
    exclude_tokens = set()
    for conj_v in conj_verbs:
        for t in conj_v.subtree:
            exclude_tokens.add(t)
            
    for token in sent:
        if token.dep_ == "cc":
            if token.head == root_verb or token.head in [v.head for v in conj_verbs]:
                exclude_tokens.add(token)
            
    clause1_tokens = [t for t in sent if t not in exclude_tokens and t.text not in [".", ",", ";", "!"]]
    clause1_text = " ".join([t.text for t in clause1_tokens]).strip()
    
    clauses = [clause1_text]
    
    for conj_v in conj_verbs:
        conj_words = [t.text for t in conj_v.subtree if t.text not in [".", ",", ";", "!"]]
        # If the coordinate clause doesn't have its own subject, prepend ROOT's subject
        has_subj = any(t.dep_ in ["nsubj", "nsubjpass"] for t in conj_v.subtree)
        if has_subj:
            conj_text = " ".join(conj_words).strip()
        else:
            conj_text = f"{subject_text} " + " ".join(conj_words).strip()
        clauses.append(conj_text)
        
    cleaned_clauses = []
    for c in clauses:
        c_clean = c.strip()
        if c_clean:
            # Capitalize and add terminal punctuation
            c_clean = c_clean[0].upper() + c_clean[1:]
            if not c_clean.endswith((".", "!", "?")):
                c_clean += "."
            cleaned_clauses.append(c_clean)
            
    return cleaned_clauses if cleaned_clauses else [sent.text.strip()]

# ──────────────────────────────────────────────────────────────────────
# Main Entry Point
# ──────────────────────────────────────────────────────────────────────

def parse_events(content: str) -> list:
    """
    Parses a block of text/conversation into a list of atomic sentences/events.
    First tries the LLM API, then falls back to a spaCy-based clause splitter.
    """
    if not content or not content.strip():
        return []
        
    # 1. Try LLM first
    llm_events = _extract_events_via_llm_api(content)
    if llm_events:
        return [e.strip() for e in llm_events if e.strip()]
        
    # 2. Fallback to spaCy
    logger.info("[EventParser] Using spaCy-based fallback clause splitter")
    nlp = load_spacy_model()
    doc = nlp(content)
    
    events = []
    for sent in doc.sents:
        try:
            split_clauses = _split_conjunction_clauses(sent)
            events.extend(split_clauses)
        except Exception as e:
            logger.warning(f"Failed to split clauses in sentence '{sent.text}': {e}")
            events.append(sent.text.strip())
            
    return [e for e in events if e.strip()]
