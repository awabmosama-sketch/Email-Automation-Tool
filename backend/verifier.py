import re
import dns.resolver

# Standard email format check (single @, valid domain labels, no leading/trailing dots)
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)

# Disposable / known-bad domains (sample — aap extend kar sakte ho)
BLOCKED_DOMAINS = {
    "tempmail.com",
    "10minutemail.com",
    "guerrillamail.com",
    "mailinator.com",
    "throwaway.email",
}


def is_valid_format(email: str) -> bool:
    """Syntax check — kya email valid format mein hai?"""
    if not email or not isinstance(email, str):
        return False
    email = email.strip()
    if len(email) > 254:
        return False
    local_part, _, domain = email.rpartition("@")
    if not local_part or len(local_part) > 64 or ".." in local_part:
        return False
    if local_part.startswith(".") or local_part.endswith("."):
        return False
    return bool(EMAIL_REGEX.match(email))


def has_mx_record(email: str) -> bool:
    """Check karta hai ke domain ke MX records exist karte hain ya nahi.
    Agar MX record nahi hai, matlab us domain pe email receive nahi hoti."""
    domain = email.split("@")[-1].lower().strip()

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(answers) > 0
    except Exception:
        return False


def is_blocked_domain(email: str) -> bool:
    """Disposable / spam-trap domains block karo (subdomains included)."""
    domain = email.split("@")[-1].lower().strip()
    return any(
        domain == blocked or domain.endswith("." + blocked)
        for blocked in BLOCKED_DOMAINS
    )


def verify(email: str) -> tuple:
    """
    Full verification.
    Returns: (is_valid, reason)
    """
    if not is_valid_format(email):
        return False, "invalid_format"

    if is_blocked_domain(email):
        return False, "blocked_domain"

    if not has_mx_record(email):
        return False, "no_mx_record"

    return True, "ok"


# Testing ke liye — jab directly run karo
if __name__ == "__main__":
    test_emails = [
        "test@gmail.com",
        "invalid-email",
        "someone@nonexistentdomain12345.com",
        "fake@mailinator.com",
    ]

    for email in test_emails:
        ok, reason = verify(email)
        status = "✅" if ok else "❌"
        print(f"{status} {email:40} → {reason}")