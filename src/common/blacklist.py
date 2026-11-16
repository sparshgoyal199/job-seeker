"""
Centralized domain and email blacklist for recruiter discovery and outreach.
Prevents scraping or emailing generic job portals, search engines, and social media platforms.
"""

GENERIC_DOMAINS = {
    # Free/Personal webmails
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    # Job boards & ATS portals
    "internshala.com", "indeed.com", "naukri.com", "linkedin.com",
    "glassdoor.com", "ambitionbox.com", "careerspage.io",
    "jobviareferral.com", "greenhouse.io", "lever.co", "workday.com",
    "myworkdayjobs.com", "smartrecruiters.com", "oraclecloud.com",
    "freshers.com", "freshersworld.com",
    # Search engines & Info sites
    "google.com", "bing.com", "wikipedia.org", "wikimedia.org",
    # Social media
    "youtube.com", "facebook.com", "instagram.com", "twitter.com", "x.com"
}

PORTAL_EMAIL_BLACKLIST = {
    "internshala.com", "bing.com", "google.com", "facebook.com", "freshers.com",
    "freshersworld.com", "indeed.com", "naukri.com", "linkedin.com", "careerspage.io",
    "wikipedia.org", "wikimedia.org", "youtube.com", "instagram.com", "twitter.com", "x.com",
    "jobviareferral.com"
}


def is_generic_domain(domain: str) -> bool:
    """Returns True if domain is a job board, search engine, social media, or generic webmail."""
    if not domain:
        return True
    d = domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return any(d == g or d.endswith("." + g) for g in GENERIC_DOMAINS)


def is_blacklisted_email(email: str) -> bool:
    """Returns True if the email address belongs to a blacklisted portal domain."""
    if not email or "@" not in email:
        return True
    domain = email.lower().strip().split("@")[-1]
    return any(domain == b or domain.endswith("." + b) for b in PORTAL_EMAIL_BLACKLIST)
