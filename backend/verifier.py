import re
import dns.resolver

# Standard email format check
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
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
    return bool(EMAIL_REGEX.match(email.strip()))


def has_mx_record(email: str) -> bool:
    """Check karta hai ke domain ke MX records exist karte hain ya nahi.
    Agar MX record nahi hai, matlab us domain pe email receive nahi hoti."""
    domain = email.split("@")[-1].lower().strip()

    try:
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except (dns.resolver.NXDOMAIN,
            dns.resolver.NoAnswer,
            dns.resolver.NoNameservers,
            dns.resolver.Timeout,
            Exception):
        return False


def is_blocked_domain(email: str) -> bool:
    """Disposable / spam-trap domains block karo."""
    domain = email.split("@")[-1].lower().strip()
    return domain in BLOCKED_DOMAINS


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