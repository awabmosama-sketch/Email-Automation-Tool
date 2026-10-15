import os
import json
import re
from groq import Groq
from dotenv import load_dotenv

try:
    from .companies import get_company_profile
except ImportError:
    from companies import get_company_profile

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing in .env")

client = Groq(api_key=GROQ_API_KEY)

MODEL = "openai/gpt-oss-120b"


def _extract_json(text):
    """Response se JSON nikaalta hai, chahe model ne extra text likha ho."""
    text = text.strip()
    # Markdown code block ho toh remove karo
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # Pehla { se aakhri } tak nikaalo
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end+1]
    return json.loads(text)


def generate_email(recipient_name, recipient_email, context=""):
    """LLM se personalized email generate karta hai."""
    prompt = f"""You are an email copywriter. Write a short, friendly, personalized email.

Recipient name: {recipient_name}
Recipient email: {recipient_email}
Context: {context or "General introduction / test email"}

Rules:
- Keep it under 100 words
- Friendly but professional tone
- No spam trigger words (free, guaranteed, urgent, act now, limited time)
- Personalize with the recipient's name
- End with a simple sign-off
- Plain text only, no markdown

Return ONLY valid JSON, no other text, in exactly this format:
{{"subject": "your subject line", "body": "your email body here"}}
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_completion_tokens=1024,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )

    text = response.choices[0].message.content.strip()

    try:
        data = _extract_json(text)
        subject = data.get("subject", "Quick note").strip()
        body = data.get("body", "").strip()
    except Exception as e:
        print(f"   [parse error] {e}")
        print(f"   [raw] {text[:200]}")
        subject = "Quick note"
        body = text

    return {"subject": subject, "body": body}


def check_spam_score(email_text):
    """LLM se spam score check karta hai (0-10)."""
    prompt = f"""Analyze this email for spam trigger words and deliverability issues.

Email:
{email_text}

Return ONLY valid JSON in exactly this format:
{{"score": <integer 0-10>, "issues": "comma-separated issues or none"}}

Score 0 = totally clean, 10 = very spammy.
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_completion_tokens=512,
        reasoning_effort="low",
        response_format={"type": "json_object"},
    )

    text = response.choices[0].message.content.strip()

    try:
        data = _extract_json(text)
        score = int(data.get("score", 0))
        issues = str(data.get("issues", "none")).strip() or "none"
    except Exception:
        score = 0
        issues = "parse_error"

    return {"score": score, "issues": issues}


def _enforce_company_name_spelling(text: str, company_name: str) -> str:
    """Ensures company name is spelled with 100% letter-for-letter precision and fixes LLM typos."""
    if not text or not company_name or len(company_name.strip()) < 3:
        return text

    clean_name = company_name.strip()
    # 1. Exact case correction (e.g. 'abdion' or 'ABDION' -> 'Abdion')
    pattern = re.compile(rf"\b{re.escape(clean_name)}\b", re.IGNORECASE)
    text = pattern.sub(clean_name, text)

    # 2. Check for 1-character typo variants (e.g., 'Abdian' for 'Abdion')
    words = re.findall(r"\b[A-Za-z0-9\-\']+\b", text)
    comp_lower = clean_name.lower()
    comp_len = len(comp_lower)

    for word in set(words):
        w_lower = word.lower()
        if w_lower == comp_lower:
            continue
        if len(w_lower) == comp_len and comp_len >= 4:
            diffs = sum(1 for a, b in zip(w_lower, comp_lower) if a != b)
            if diffs == 1 and w_lower[:2] == comp_lower[:2]:
                typo_pattern = re.compile(rf"\b{re.escape(word)}\b")
                text = typo_pattern.sub(clean_name, text)

    return text


def generate_pitch_email(
    recipient_name: str,
    recipient_email: str,
    recipient_phone: str,
    company_name: str,
    target_role: str,
    target_industry: str,
    details: str = "",
) -> dict:
    """Generates a personalized pitch email from a pitching company to a target lead."""
    profile = get_company_profile(company_name)
    services_list = "\n".join(f"- {s}" for s in profile.get("services", []))
    value_prop = profile.get("value_proposition", "")
    tone = profile.get("tone", "friendly-professional")
    signature = profile.get("signature", "")

    system_prompt = f"""You are an expert sales copywriter crafting a personalized pitch email on behalf of {company_name or 'our company'}.

Pitching Company Details:
Company: {company_name or 'Our Company'}
Services:
{services_list}
Value Proposition: {value_prop}
Brand Tone: {tone}
Official Signature block:
{signature}

Strict Rules:
- EXACT COMPANY NAME SPELLING: The pitching company name is "{company_name}". You MUST use the EXACT, letter-for-letter spelling "{company_name}". Never change vowels, alter letters, or introduce phonetic typos (e.g., write "{company_name}", never misspelled variations).
- Reference the target recipient's name, role ({target_role}), and industry ({target_industry}) naturally.
- Choose and pitch ONLY the 1 or 2 most relevant services that solve specific problems for their role and industry. Do NOT dump all services.
- Do NOT invent or hallucinate services outside the provided list.
- Keep the email warm, professional, and strictly under 150 words.
- Avoid spam trigger words (e.g., 'free', 'guaranteed', 'risk-free', 'urgent', 'act now', 'buy now', 'limited time').
- End the email with the EXACT official signature block provided above.
- Return ONLY valid JSON with keys "subject" and "body". No conversational filler, no markdown code fence.
"""

    user_prompt = f"""Target Lead Information:
- Recipient Name: {recipient_name}
- Recipient Email: {recipient_email}
- Recipient Phone: {recipient_phone or 'Not provided'}
- Target Role: {target_role}
- Target Industry: {target_industry}
- Additional Context / Details: {details or 'None'}

Return ONLY a valid JSON object in exactly this format:
{{"subject": "A compelling, relevant subject line", "body": "The email message ending with the official signature block"}}
"""

    def _invoke(strict: bool = False) -> str:
        sys_content = system_prompt
        usr_content = user_prompt
        if strict:
            sys_content += (
                "\nCRITICAL: Your output MUST be strictly parseable JSON conforming to RFC 8259."
                " Do not include code fences, markdown, or extra explanations."
            )
            usr_content += "\nEnsure output starts with { and ends with }."

        res = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_content},
                {"role": "user", "content": usr_content},
            ],
            temperature=0.6 if not strict else 0.3,
            max_completion_tokens=1024,
            reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        return res.choices[0].message.content.strip()

    # Attempt 1
    raw_text = _invoke(strict=False)
    try:
        data = _extract_json(raw_text)
    except Exception:
        # Retry once with stricter instruction
        try:
            raw_text = _invoke(strict=True)
            data = _extract_json(raw_text)
        except Exception as retry_err:
            raise RuntimeError(
                f"LLM failed to return valid JSON pitch after retry: {retry_err}"
            ) from retry_err

    subject = str(data.get("subject", "")).strip() or f"Collaboration with {company_name}"
    body = str(data.get("body", "")).strip()
    if not body:
        raise RuntimeError("LLM generated an empty email body.")

    subject = _enforce_company_name_spelling(subject, company_name)
    body = _enforce_company_name_spelling(body, company_name)

    spam_result = check_spam_score(body)
    spam_score = spam_result.get("score", 0)

    return {
        "subject": subject,
        "body": body,
        "spam_score": spam_score,
    }


if __name__ == "__main__":
    print("Testing LLM email generation...\n")

    result = generate_email(
        recipient_name="Awab",
        recipient_email="awabmosama@gmail.com",
        context="Introducing a new email automation tool"
    )

    print("SUBJECT:", result["subject"])
    print("\nBODY:\n", result["body"])

    print("\nChecking spam score...")
    spam = check_spam_score(result["body"])
    print(f"Score: {spam['score']}/10")
    print(f"Issues: {spam['issues']}")