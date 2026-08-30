"""Phrases that mark a message as machine-written to a careful reader.

One list, used by templates.py's lint. Matching is case-insensitive substring.
Edit here only.
"""

SLOP_BANNED = [
    "I hope this email finds you well",
    "I wanted to reach out",
    "I wanted to follow up on",
    "Just wanted to touch base",
    "I'd be happy to",
    "I'd love to",
    "Please don't hesitate to",
    "Thank you for your understanding",
    "I appreciate your patience",
    "Moving forward",
    "Going forward",
    "As discussed",
    "Per our conversation",
    "I trust this helps",
    "I hope this clarifies",
    "Best regards",
    "Warm regards",
    "Kind regards",
    "Great question!",
    "I'd be happy to discuss further",
    "I'd love to explore this",
    "Per my last email",
    "It's worth noting that",
    "It goes without saying",
    "At the end of the day",
    "With that being said",
    "foster collaboration",
    "cutting-edge",
    "delve into",
    "multifaceted",
    "synergy",
    "comprehensive",
    "robust",
    "streamline",
    "leverage",
    "utilize",
    "facilitate",
    "endeavor",
    "thrilled",
    "excited",
    "delighted",
    "genuinely",
    "honestly",
    "per our last conversation",
    "please don't hesitate",
    "indeed,",
    "moreover,",
    "furthermore,",
    "thrilled to",
    "excited to",
    "delighted to",
]

EM_DASH_PATTERNS = [
    "—",   # em dash
    "–",   # en dash used as em dash
    " -- ",     # double hyphen with spaces
    " --- ",    # triple hyphen with spaces
]
