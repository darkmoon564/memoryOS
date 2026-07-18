import re
from datetime import datetime, timedelta, timezone

def parse_temporal_window(query: str, current_time: datetime = None) -> tuple[datetime | None, datetime | None]:
    """
    Parses relative and absolute month keywords from a query string and returns
    a timezone-aware UTC datetime tuple (start_time, end_time) representing the target window.
    """
    if current_time is None:
        current_time = datetime.now(timezone.utc)
    else:
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
            
    query_lower = query.lower()

    # Explicit ISO-style dates and years are common in memory questions and
    # must use the event's occurrence time rather than the ingestion time.
    date_match = re.search(r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b", query_lower)
    if date_match:
        try:
            day = datetime.fromisoformat(date_match.group(0)).replace(tzinfo=timezone.utc)
            return day, day + timedelta(days=1) - timedelta(microseconds=1)
        except ValueError:
            pass

    year_match = re.search(r"\b(19|20)\d{2}\b", query_lower)
    if year_match:
        year = int(year_match.group(0))
        return (
            datetime(year, 1, 1, tzinfo=timezone.utc),
            datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        )
    
    # Yesterday
    if "yesterday" in query_lower:
        start_of_today = datetime(current_time.year, current_time.month, current_time.day, tzinfo=timezone.utc)
        start_of_yesterday = start_of_today - timedelta(days=1)
        end_of_yesterday = start_of_today - timedelta(microseconds=1)
        return start_of_yesterday, end_of_yesterday
        
    # Today
    if "today" in query_lower:
        start_of_today = datetime(current_time.year, current_time.month, current_time.day, tzinfo=timezone.utc)
        return start_of_today, current_time
        
    # Last week / 7 days
    if "last week" in query_lower or "7 days" in query_lower:
        start_time = current_time - timedelta(days=7)
        return start_time, current_time
        
    # Recent / last 3 days
    if "recent" in query_lower or "last 3 days" in query_lower:
        start_time = current_time - timedelta(days=3)
        return start_time, current_time
        
    # Month parsing (e.g. "june", "may", "january", etc.)
    months = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12
    }
    
    for m_name, m_num in months.items():
        pattern = rf"\b{m_name}\b"
        if re.search(pattern, query_lower):
            target_year = current_time.year
            if m_num > current_time.month:
                target_year -= 1
                
            # Find last day of target month
            if m_num == 12:
                last_day = 31
            else:
                next_month = datetime(target_year, m_num + 1, 1, tzinfo=timezone.utc)
                last_day = (next_month - timedelta(days=1)).day
                
            start_time = datetime(target_year, m_num, 1, 0, 0, 0, tzinfo=timezone.utc)
            end_time = datetime(target_year, m_num, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
            return start_time, end_time
            
    return None, None
