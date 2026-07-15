import os
import json
import requests
import spacy
from memoryos.config import logger

nlp = None

# ──────────────────────────────────────────────────────────────────────
# spaCy Model Loader
# ──────────────────────────────────────────────────────────────────────

def load_spacy_model():
    """Dynamically load the lightweight spaCy model on first run. Does not perform runtime downloads."""
    global nlp
    if nlp is not None:
        return nlp
        
    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        logger.warning(
            "spaCy model 'en_core_web_sm' is not pre-installed. "
            "Offline extraction will fall back to simple regex-based parser. "
            "To resolve, run: python -m spacy download en_core_web_sm"
        )
        nlp = None
    return nlp

def _regex_relationship_extractor(content: str) -> dict:
    """Fallback rule-based relationship parser when spaCy is not available."""
    data = {"entities": [], "relationships": []}
    content_lower = content.lower().strip()
    
    import re
    m_work = re.search(r"(\b[a-z0-9_\-]+)\s+(?:works\s+at|employed\s+at)\s+([a-z0-9_\-\s\.]+)", content_lower)
    if m_work:
        subj = m_work.group(1).strip()
        obj = m_work.group(2).rstrip(".").strip()
        data["entities"].extend([{"name": subj, "type": "Person"}, {"name": obj, "type": "Organization"}])
        data["relationships"].append({"source": subj, "target": obj, "type": "WORKS_AT", "properties": {}})
        
    m_live = re.search(r"(\b[a-z0-9_\-]+)\s+(?:lives\s+in|located\s+in)\s+([a-z0-9_\-\s\.]+)", content_lower)
    if m_live:
        subj = m_live.group(1).strip()
        obj = m_live.group(2).rstrip(".").strip()
        data["entities"].extend([{"name": subj, "type": "Person"}, {"name": obj, "type": "Location"}])
        data["relationships"].append({"source": subj, "target": obj, "type": "LIVES_IN", "properties": {}})
        
    m_use = re.search(r"(\b[a-z0-9_\-]+)\s+(?:uses|using)\s+([a-z0-9_\-\s\.]+)", content_lower)
    if m_use:
        subj = m_use.group(1).strip()
        obj = m_use.group(2).rstrip(".").strip()
        data["entities"].extend([{"name": subj, "type": "Person"}, {"name": obj, "type": "Technology"}])
        data["relationships"].append({"source": subj, "target": obj, "type": "USES", "properties": {}})
        
    m_like = re.search(r"(\b[a-z0-9_\-]+)\s+(?:loves|likes|interested\s+in)\s+([a-z0-9_\-\s\.]+)", content_lower)
    if m_like:
        subj = m_like.group(1).strip()
        obj = m_like.group(2).rstrip(".").strip()
        data["entities"].extend([{"name": subj, "type": "Person"}, {"name": obj, "type": "Entity"}])
        data["relationships"].append({"source": subj, "target": obj, "type": "INTERESTED_IN", "properties": {}})
        
    return data

# ──────────────────────────────────────────────────────────────────────
# LLM Extraction Prompt
# ──────────────────────────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = (
    "You are an expert entity extraction system. Analyze the sentence and extract entities and relationships. "
    "Return the output STRICTLY in JSON format without markdown wraps, matching this JSON schema:\n"
    "{\n"
    "  \"entities\": [{\"name\": \"entity_name\", \"type\": \"Person|Organization|Language|Technology|Product|Location\"}],\n"
    "  \"relationships\": [{\"source\": \"entity_name\", \"target\": \"entity_name\", \"type\": \"WORKS_AT|INTERESTED_IN|USES|LIVES_IN|KNOWS\", \"properties\": {}}]\n"
    "}\n"
    "Ensure entity names are singular and lowercase."
)

# ──────────────────────────────────────────────────────────────────────
# LLM API Extraction (OpenAI-compatible or Ollama)
# ──────────────────────────────────────────────────────────────────────

def _extract_via_llm_api(content: str) -> dict | None:
    """
    Attempts extraction using a configurable LLM API.
    
    Supports two modes (set via environment variables):
    
    1. OpenAI-compatible API (works with OpenAI, Groq, Together, vLLM, LiteLLM):
       LLM_API_BASE=https://api.openai.com/v1
       LLM_API_KEY=sk-...
       LLM_MODEL=gpt-4o-mini
    
    2. Ollama (local LLM, legacy):
       OLLAMA_URL=http://localhost:11434
       OLLAMA_MODEL=llama3.2
    
    Returns the parsed extraction dict, or None if the LLM call fails.
    """
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
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": content}
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"}
            }
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code == 200:
                raw_text = response.json()["choices"][0]["message"]["content"]
                result = json.loads(raw_text)
                logger.info(f"[Extractor] Entity extraction completed via LLM API ({llm_model})")
                return result
            else:
                logger.warning(f"LLM API returned status {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"LLM API extraction failed: {e}")
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
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
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
            logger.info(f"[Extractor] Entity extraction completed via Ollama ({ollama_model})")
            return result
    except Exception as e:
        logger.warning(f"Ollama extraction failed: {e}. Using spaCy-based fallback parser.")
    
    return None

# ──────────────────────────────────────────────────────────────────────
# Main Extraction Entry Point
# ──────────────────────────────────────────────────────────────────────

def extract_entities_and_relationships(content: str) -> dict:
    """
    Extracts entities and relationships from memory content.
    
    Extraction priority:
    1. LLM API (OpenAI-compatible or Ollama) — highest quality, requires network/model
    2. spaCy dependency parser — offline fallback, grammatical SVO extraction
    """
    # Try LLM first
    llm_result = _extract_via_llm_api(content)
    if llm_result:
        return llm_result
    
    # Fallback to spaCy
    logger.info("[Extractor] Using spaCy dependency parser fallback")
    return _spacy_dependency_extractor(content)

# ──────────────────────────────────────────────────────────────────────
# spaCy SVO Dependency Extractor (Offline Fallback)
# ──────────────────────────────────────────────────────────────────────

# Maps spaCy NER labels to our entity type taxonomy
NER_TYPE_MAP = {
    "ORG": "Organization",
    "GPE": "Location",
    "LOC": "Location",
    "PERSON": "Person",
    "PRODUCT": "Technology",
    "WORK_OF_ART": "Product",
    "LANGUAGE": "Language",
    "NORP": "Organization",
    "FAC": "Location",
}

def _resolve_entity_type(doc, token, fallback: str = "Entity") -> str:
    """Resolve entity type using spaCy's built-in NER labels if available."""
    for ent in doc.ents:
        if token.idx >= ent.start_char and token.idx < ent.end_char:
            return NER_TYPE_MAP.get(ent.label_, fallback)
    return fallback

def get_chunk_text(doc, token) -> str:
    """Retrieve full noun chunk containing the token, cleaned of leading articles/determiners."""
    for chunk in doc.noun_chunks:
        if token in chunk:
            words = chunk.text.split()
            # Clean common determiners/possessives from start
            if words and words[0].lower() in ["a", "an", "the", "my", "our", "his", "her", "their", "your"]:
                return " ".join(words[1:]).strip()
            return chunk.text.strip()
    return token.text.strip()

def _spacy_dependency_extractor(content: str) -> dict:
    """
    A spaCy-based Subject-Verb-Object (SVO) dependency extraction fallback.
    Traverses the grammatical dependency tree to find relationships and normalize entities.
    Uses spaCy's built-in NER to resolve entity types (Person, Organization, Location, etc.).
    """
    nlp = load_spacy_model()
    if nlp is None:
        return _regex_relationship_extractor(content)
    doc = nlp(content)
    
    data = {"entities": [], "relationships": []}
    
    # Verb lemmas mapped to unified database relationship types
    verb_mappings = {
        "work": "WORKS_AT",
        "join": "WORKS_AT",
        "employ": "WORKS_AT",
        "live": "LIVES_IN",
        "move": "LIVES_IN",
        "relocate": "LIVES_IN",
        "reside": "LIVES_IN",
        "use": "USES",
        "code": "USES",
        "program": "USES",
        "build": "USES",
        "write": "USES",
        "switch": "USES",
        "prefer": "INTERESTED_IN",
        "love": "INTERESTED_IN",
        "like": "INTERESTED_IN",
        "hate": "INTERESTED_IN",
        "dislike": "INTERESTED_IN",
        "enjoy": "INTERESTED_IN",
        "study": "STUDIES_AT",
        "graduate": "STUDIES_AT",
        "attend": "STUDIES_AT",
        "marry": "MARRIED_TO",
        "drive": "DRIVES",
        "speak": "SPEAKS",
        "know": "KNOWS",
    }
    
    extracted_rels = set()
    
    for token in doc:
        lemma = token.lemma_.lower()
        # Find trigger verbs in the sentence
        if token.pos_ in ["VERB", "AUX"] and lemma in verb_mappings:
            rel_type = verb_mappings[lemma]
            
            # 1. Negation Check: Skip negated statements (e.g., "Bob does not use VSCode")
            is_negated = any(child.dep_ == "neg" for child in token.children)
            if token.head != token and token.head.pos_ in ["VERB", "AUX"]:
                if any(c.dep_ == "neg" for c in token.head.children):
                    is_negated = True
            
            if is_negated:
                continue
                
            # 2. Extract Subject (nominal subject 'nsubj' or passive subject 'nsubjpass')
            subject_tok = None
            for child in token.children:
                if child.dep_ in ["nsubj", "nsubjpass"]:
                    subject_tok = child
                    break
            # Fallback to parent verb subject if nested
            if not subject_tok and token.head != token:
                for child in token.head.children:
                    if child.dep_ in ["nsubj", "nsubjpass"]:
                        subject_tok = child
                        break
            
            if not subject_tok:
                continue
                
            # 3. Extract Target Object (dobj, prep -> pobj, attr)
            target_tok = None
            for child in token.children:
                if child.dep_ == "dobj":
                    target_tok = child
                    break
                elif child.dep_ == "prep":
                    for sub_child in child.children:
                        if sub_child.dep_ == "pobj":
                            target_tok = sub_child
                            break
                    if target_tok:
                        break
                elif child.dep_ == "attr":
                    target_tok = child
                    break
            
            if not target_tok:
                continue
                
            # 4. Resolve text boundaries using noun chunks
            subject_text = get_chunk_text(doc, subject_tok)
            target_text = get_chunk_text(doc, target_tok)
            
            # Normalize pronouns (canonicalize "I", "my" to "user")
            if subject_text.lower() in ["i", "my", "me", "we", "he", "she", "they"]:
                subject_text = "user"
                
            if subject_text and target_text:
                source_clean = subject_text.lower().strip()
                target_clean = target_text.lower().strip()
                
                # Filter out stopword targets and self-relations
                if source_clean == target_clean or target_clean in ["it", "them", "him", "her", "that", "this", "something", "anything"]:
                    continue
                    
                rel_key = (source_clean, rel_type, target_clean)
                if rel_key not in extracted_rels:
                    extracted_rels.add(rel_key)
                    
                    # Resolve entity types using spaCy NER labels
                    source_type = "User" if source_clean == "user" else _resolve_entity_type(doc, subject_tok, "Person")
                    target_type = _resolve_entity_type(doc, target_tok, "Entity")
                    
                    data["entities"].append({
                        "name": source_clean,
                        "type": source_type
                    })
                    data["entities"].append({
                        "name": target_clean,
                        "type": target_type
                    })
                    data["relationships"].append({
                        "source": source_clean,
                        "target": target_clean,
                        "type": rel_type,
                        "properties": {}
                    })
                    
    return data
