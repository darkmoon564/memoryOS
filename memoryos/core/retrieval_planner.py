import re
from memoryos.services.extractor import load_spacy_model

# Common English stopwords to filter keywords
STOPWORDS = {
    'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you', "you're", "you've", "you'll", "you'd",
    'your', 'yours', 'yourself', 'yourselves', 'he', 'him', 'his', 'himself', 'she', "she's", 'her', 'hers',
    'herself', 'it', "it's", 'its', 'itself', 'they', 'them', 'their', 'theirs', 'themselves', 'what', 'which',
    'who', 'whom', 'this', 'that', "that'll", 'these', 'those', 'am', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'having', 'do', 'does', 'did', 'doing', 'a', 'an', 'the', 'and', 'but', 'if',
    'or', 'because', 'as', 'until', 'while', 'of', 'at', 'by', 'for', 'with', 'about', 'against', 'between',
    'into', 'through', 'during', 'before', 'after', 'above', 'below', 'to', 'from', 'up', 'down', 'in', 'out',
    'on', 'off', 'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there', 'when', 'where', 'why',
    'how', 'all', 'any', 'both', 'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor', 'not',
    'only', 'own', 'same', 'so', 'than', 'too', 'very', 's', 't', 'can', 'will', 'just', 'don', "don't",
    'should', "should've", 'now', 'd', 'll', 'm', 'o', 're', 've', 'y', 'ain', 'aren', "aren't", 'couldn',
    "couldn't", 'didn', "didn't", 'doesn', "doesn't", 'hadn', "hadn't", 'hasn', "hasn't", 'haven', "haven't",
    'isn', "isn't", 'ma', 'mightn', "mightn't", 'mustn', "mustn't", 'needn', "needn't", 'shan', "shan't",
    'shouldn', "shouldn't", 'wasn', "wasn't", 'weren', "weren't", 'won', "won't", 'wouldn', "wouldn't",
    'does', 'do', 'what', 'where', 'why', 'who', 'how', 'tell', 'show', 'find', 'get', 'list', 'about'
}

def plan_retrieval(query: str) -> dict:
    """
    Parses user query to extract entities, intent, and clean search keywords.
    """
    nlp = load_spacy_model()
    doc = nlp(query)
    
    # 1. Extract Entities
    entities = set()
    # Check named entities from spaCy
    for ent in doc.ents:
        entities.add(ent.text.lower().strip())
        
    # Fallback to proper nouns and noun chunks (since user queries are often lowercase)
    for token in doc:
        if token.pos_ in ["PROPN", "NOUN"] and token.text.lower() not in STOPWORDS:
            entities.add(token.text.lower().strip())
            
    for chunk in doc.noun_chunks:
        # Add clean noun chunk text
        chunk_clean = chunk.text.lower().strip()
        # Filter out leading articles
        chunk_clean = re.sub(r'^(a|an|the|my|your|our|their|his|her)\s+', '', chunk_clean)
        if chunk_clean and chunk_clean not in STOPWORDS:
            entities.add(chunk_clean)
            
    # Clean entity names (remove non-alphanumeric chars at boundary)
    cleaned_entities = []
    for ent in entities:
        cleaned = re.sub(r'^\W+|\W+$', '', ent)
        if cleaned and cleaned not in STOPWORDS and len(cleaned) > 1:
            cleaned_entities.append(cleaned)
            
    # 2. Classify Intent
    query_lower = query.lower()
    intent = "factual_lookup"
    
    preference_keywords = ["like", "prefer", "love", "hate", "favorite", "dislike", "interest", "want"]
    temporal_keywords = ["when", "date", "yesterday", "last week", "recent", "valid", "history", "time", "before", "after", "ago"]
    
    if any(k in query_lower for k in preference_keywords):
        intent = "preference_query"
    elif any(k in query_lower for k in temporal_keywords):
        intent = "temporal_query"
        
    # 3. Extract Keywords (clean content tokens)
    keywords = []
    for token in doc:
        if not token.is_stop and not token.is_punct and token.text.lower() not in STOPWORDS:
            # Keep alphanumeric terms or terms with dash
            clean_tok = re.sub(r'^\W+|\W+$', '', token.text.lower())
            if clean_tok and len(clean_tok) > 1:
                keywords.append(clean_tok)
                
    return {
        "entities": sorted(list(set(cleaned_entities))),
        "intent": intent,
        "keywords": sorted(list(set(keywords)))
    }
