import os
import re
import json
import requests
import spacy
from memoryos.config import logger

nlp = None

def load_spacy_model():
    """Dynamically load or download the lightweight spaCy model on first run."""
    global nlp
    if nlp is not None:
        return nlp
        
    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        logger.info("Downloading lightweight spaCy model (en_core_web_sm)...")
        from spacy.cli import download
        download("en_core_web_sm")
        nlp = spacy.load("en_core_web_sm")
    return nlp

def extract_entities_and_relationships(content: str) -> dict:
    """
    Extracts entities and relationships from the memory content using Ollama (Llama 3.2 3B).
    Falls back to a spaCy dependency-based grammatical parser if Ollama is unavailable.
    """
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model_name = os.getenv("OLLAMA_MODEL", "llama3.2")
    
    system_prompt = (
        "You are an expert entity extraction system. Analyze the sentence and extract entities and relationships. "
        "Return the output STRICTLY in JSON format without markdown wraps, matching this JSON schema:\n"
        "{\n"
        "  \"entities\": [{\"name\": \"entity_name\", \"type\": \"Person|Organization|Language|Technology|Product|Location\"}],\n"
        "  \"relationships\": [{\"source\": \"entity_name\", \"target\": \"entity_name\", \"type\": \"WORKS_AT|INTERESTED_IN|USES|LIVES_IN|KNOWS\", \"properties\": {}}]\n"
        "}\n"
        "Ensure entity names are singular and lowercase."
    )

    try:
        response = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content}
                ],
                "format": "json",
                "stream": False
            },
            timeout=3.0
        )
        if response.status_code == 200:
            raw_text = response.json().get("message", {}).get("content", "")
            return json.loads(raw_text)
    except Exception as e:
        logger.warning(f"Ollama extraction failed: {e}. Using spaCy-based fallback parser.")
    
    return _spacy_dependency_extractor(content)

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
    """
    nlp = load_spacy_model()
    doc = nlp(content)
    
    data = {"entities": [], "relationships": []}
    
    # Verb lemmas mapped to unified database relationship types
    verb_mappings = {
        "work": "WORKS_AT",
        "live": "LIVES_IN",
        "move": "LIVES_IN",
        "use": "USES",
        "code": "USES",
        "program": "USES",
        "prefer": "INTERESTED_IN",
        "love": "INTERESTED_IN",
        "like": "INTERESTED_IN",
        "hate": "INTERESTED_IN",
        "dislike": "INTERESTED_IN",
        "enjoy": "INTERESTED_IN"
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
                    
                    data["entities"].append({
                        "name": source_clean,
                        "type": "Person" if source_clean != "user" else "User"
                    })
                    data["entities"].append({
                        "name": target_clean,
                        "type": "Entity"
                    })
                    data["relationships"].append({
                        "source": source_clean,
                        "target": target_clean,
                        "type": rel_type,
                        "properties": {}
                    })
                    
    return data
