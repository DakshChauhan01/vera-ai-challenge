"""Language-preference resolution.

We only ever produce two registers of English:
  - "en"     — plain English
  - "hi_en"  — natural Hindi-English (Hinglish) code-mix, Romanized, matching
               the dataset's own `code_mix: "hindi_english_natural"` voice
               field and the brief's worked examples.

For customers whose `language_pref` names a *regional* code-mix we don't
have (te-en, kn-en, ta-en, mr), we deliberately do NOT fabricate Telugu /
Kannada / Tamil / Marathi text — inventing words in a script/language we
have no verified vocabulary for is a worse failure mode than staying in
English. We still honor the preference by using a warm, first-name-led
register and by saying so plainly in the rationale, and this tradeoff is
called out in the README. If the account language pref explicitly says
"hi" (Hindi) or contains "mix"/"hi-en", we use the hi_en templates.
"""

from __future__ import annotations

from typing import Optional


def _lang_signal(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.strip().lower()


def resolve_language(merchant: dict, customer: Optional[dict]) -> str:
    """Returns "hi_en" or "en".

    Customer-facing messages honor the *customer's* language_pref (this is
    who will read the message). Merchant-facing messages honor the
    merchant's identity.languages list.
    """
    if customer:
        pref = _lang_signal(customer.get("identity", {}).get("language_pref"))
        if "hi" in pref or "mix" in pref:
            return "hi_en"
        if pref in ("english", "en", ""):
            # Regional mixes (te-en, kn-en, ta-en, mr) fall through to English
            # per the module docstring — we don't fabricate those languages.
            if pref == "":
                # No preference on file: fall back to merchant's own languages.
                return resolve_language(merchant, None)
            return "en"
        return "en"

    languages = merchant.get("identity", {}).get("languages") or []
    languages = [_lang_signal(l) for l in languages]
    if "hi" in languages:
        return "hi_en"
    return "en"


def merchant_first_name(merchant: dict) -> str:
    """Alias of owner_or_business_name — the name most kind-specific
    templates use as the salutation."""
    return owner_or_business_name(merchant)


def owner_or_business_name(merchant: dict) -> str:
    """Prefer the owner's first name (case studies consistently score higher
    for "Dr. Meera" / "Suresh" / "Karthik" style salutations than a bare
    business name), falling back to the business name if unknown."""
    identity = merchant.get("identity", {})
    first = identity.get("owner_first_name")
    if first:
        # Dentists get a "Dr." honorific per category voice salutation_examples.
        category_slug = merchant.get("category_slug", "")
        if category_slug == "dentists" and not str(first).lower().startswith("dr"):
            return f"Dr. {first}"
        return str(first)
    return identity.get("name", "there")
