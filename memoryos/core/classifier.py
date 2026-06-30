import re

def classify_memory(content: str) -> str:
    """Classifies memory content into FACTUAL, EPISODIC, or PREFERENCE."""
    content_lower = content.lower()
    
    preference_signals = [
        "prefers", "loves", "hates", "favorite", "always", "never",
        "likes", "dislikes", "enjoys", "avoids", "rather", "prefer",
        "can't stand", "passionate about"
    ]
    for signal in preference_signals:
        if signal in content_lower:
            return "PREFERENCE"
    
    factual_patterns = [
        r"\b(works? at|employed at|employed by)\b",
        r"\b(lives? in|located in|based in|from)\b",
        r"\b(is a|is an|was a|was an)\b",
        r"\b(uses?|using|built with)\b",
        r"\b(knows?|speaks?|fluent in)\b",
        r"\b(born in|graduated from|studied at)\b",
        r"\b(allergic to|diagnosed with)\b",
    ]
    for pattern in factual_patterns:
        if re.search(pattern, content_lower):
            return "FACTUAL"
    
    return "EPISODIC"
