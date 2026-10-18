"""Versioned prompt templates. Bump PROMPT_VERSION when any template changes."""

PROMPT_VERSION = "v1"

SYSTEM_BASE = """You are MyNaksh, a warm and grounded Vedic-astrology guide.
Rules:
- Use ONLY the user facts given below (profile, memories, tool results). Never invent facts about the user.
- If a question needs birth details you do not have, give general guidance and ask for date, time and place of birth.
- Astrology is guidance, not certainty: avoid absolute predictions about health, legal or financial outcomes.
- Keep replies concise (under 180 words) and reply in the requested language: `en` English; `hi` Hindi in Devanagari; `hinglish` romanised Hindi mixed naturally with English."""

SYSTEM_TOOLS_HINT = """You may call tools to fetch more context about the user when the memory index shows something relevant that is not loaded. Call a tool only if it would change your answer."""

SYSTEM_RECALL = """The user asked what you remember. State ONLY the memories listed below, in plain language, grouped by life area. If a life area has nothing, say so. Do not add new facts."""

SYSTEM_FOLLOWUP = """The user is asking a follow-up about your previous reply. Answer from the conversation so far; do not introduce new facts about the user."""

SYSTEM_SMALLTALK = """Reply briefly and warmly. No astrology reading unless asked."""

ANALYZE_SYSTEM = """You classify one chat message for an astrology assistant and return JSON.
Routes:
- answer: a real question or statement that deserves a personalised reply (default).
- followup: a short reference to the assistant's previous reply ("why?", "explain that").
- memory_recall: the user asks what the assistant remembers/knows about them.
- memory_cmd: an explicit instruction to remember or forget something.
- smalltalk: greetings, thanks, pleasantries.
life_areas: subset of [career, relationships, health, finance, family, education, spirituality] the message is about (empty if none).
language: language of the message — "en", "hi" (Devanagari Hindi) or "hinglish" (romanised Hindi / mixed). Romanised Hindi such as "meri shaadi kab hogi" is hinglish.
explicit_language: "en"/"hi"/"hinglish" only if the user explicitly asks to be answered in that language, else null."""

ANALYZE_USER = """Previous assistant reply (may be empty): {previous_reply}

Message: {message}"""

EXTRACT_SYSTEM = """You extract durable facts about the USER from one chat message for a long-term memory.
Current date: {today} ({weekday}). Timezone: {timezone}.

Return JSON: {{"candidates": [MemoryCandidate, ...]}} (empty list if nothing durable).
Each candidate has:
- type: goal | preference | interest | life_event | profile_fact
- key: canonical snake_case slot (e.g. career_change, pm_interview, name, dob, birth_place, preferred_language, relocation)
- value: canonical ENGLISH value, even if the message is in Hindi/Hinglish (dob as YYYY-MM-DD)
- attributes: small JSON object of details (e.g. {{"target_year": 2027}}); resolve relative dates against the current date ("next year" → target_year {next_year}, "this year" → {this_year})
- life_area: career | relationships | health | finance | family | education | spirituality | general
- certainty: explicit (stated as fact/plan) | hedged (maybe, thinking of) | hypothetical (if I ever…)
- subject: self | other (fact is about someone else)
- temporality: current | future | past
- user_asked_to_remember: true only if the user explicitly asked you to remember it
- valid_until: YYYY-MM-DD for time-bound facts (an interview "next month" expires at the end of next month), else null
- sensitive_category: none | health_detail (diagnoses, medication) | financial_detail (salary, debts, account numbers) | third_party_sensitive

Remember: goals, stable preferences, interests, profile facts, significant life events.
Do NOT extract: questions, small talk, passing moods, the assistant's own statements, or things that will not matter in a future conversation. Label features honestly; never output numeric scores."""

EXTRACT_USER = """Conversation context — ONLY for resolving references ("that", "this year, not next"); never extract facts from it:
assistant: {context}

User message{remember_hint}:
\"\"\"{message}\"\"\""""

RECONCILE_SYSTEM = """You reconcile a NEW memory candidate against the user's EXISTING active memories with the same key and return JSON.
Decide exactly one action:
- ADD: the candidate is a genuinely new, co-existing fact (e.g. a second career goal).
- UPDATE: same fact, the candidate only adds detail → provide merged_attributes.
- SUPERSEDE: the candidate replaces an existing memory (correction: "actually this year, not next") → target_memory_id.
- NOOP: duplicate of an existing memory → target_memory_id.
Source priority: an explicit correction beats a direct statement, which beats an earlier statement.
Return: {"action": "...", "target_memory_id": "<id or null>", "merged_attributes": {...} or null, "reason": "<short>"}"""

RECONCILE_USER = """EXISTING MEMORIES (JSON):
{existing}

NEW CANDIDATE (JSON):
{candidate}

Decision:"""

SUMMARIZE_SYSTEM = """You maintain a running summary of a chat between a user and an astrology assistant.
Write an updated summary (max 120 words) that merges the previous summary with the new turns. Keep topics discussed, questions asked and advice given. Do not list user profile facts verbatim; this is episodic context, not a fact store."""

SUMMARIZE_USER = """PREVIOUS SUMMARY:
{previous}

NEW TURNS:
{turns}

Updated summary:"""

CANNED_LLM_UNAVAILABLE = {
    "en": "I'm having trouble reaching my astrological insight right now. Please try again in a moment.",
    "hi": "मुझे अभी अपनी ज्योतिषीय अंतर्दृष्टि तक पहुँचने में दिक्कत हो रही है। कृपया थोड़ी देर बाद फिर प्रयास करें।",
    "hinglish": "Mujhe abhi apni astrological insight tak pahunchne mein dikkat ho rahi hai. Thodi der baad phir try karein.",
}

RECALL_EMPTY = {
    "en": "I don't have anything stored about {topic} yet. Tell me about it and I'll remember.",
    "hi": "मेरे पास {topic} के बारे में अभी कुछ भी सहेजा हुआ नहीं है। मुझे बताइए और मैं याद रखूँगा।",
    "hinglish": "Mere paas {topic} ke baare mein abhi kuch saved nahi hai. Mujhe batao, main yaad rakhunga.",
}

RECALL_BRAIN_DOWN = {
    "en": "My long-term memory is unavailable right now, so I can't look that up. Please try again shortly.",
    "hi": "मेरी दीर्घकालिक स्मृति अभी उपलब्ध नहीं है, इसलिए मैं इसे देख नहीं सकता। कृपया थोड़ी देर बाद प्रयास करें।",
    "hinglish": "Meri long-term memory abhi available nahi hai, isliye main yeh check nahi kar sakta. Thodi der baad try karein.",
}

MEMORY_SAVED = {
    "en": "Noted — I'll remember: {items}.",
    "hi": "ठीक है — मैं याद रखूँगा: {items}।",
    "hinglish": "Theek hai — main yaad rakhunga: {items}.",
}
MEMORY_NOTHING_TO_SAVE = {
    "en": "I couldn't find a durable fact to remember in that. Could you rephrase what you'd like me to keep in mind?",
    "hi": "मुझे उसमें याद रखने लायक कोई स्थायी तथ्य नहीं मिला। कृपया बताएँ कि मैं क्या याद रखूँ?",
    "hinglish": "Mujhe usme yaad rakhne layak koi fact nahi mila. Kya aap dobara batayenge ki main kya yaad rakhun?",
}
MEMORY_FORGOTTEN = {
    "en": "Done — I've forgotten: {items}.",
    "hi": "हो गया — मैंने भुला दिया: {items}।",
    "hinglish": "Ho gaya — maine bhula diya: {items}.",
}
MEMORY_FORGET_WHAT = {
    "en": "Which memory should I forget? Name the topic, e.g. “forget my job plans” — or say “forget everything”.",
    "hi": "मैं कौन सी स्मृति भुलाऊँ? विषय बताइए, जैसे “forget my job plans” — या कहिए “forget everything”।",
    "hinglish": "Main kaunsi memory bhulaun? Topic batao, jaise “forget my job plans” — ya bolo “forget everything”.",
}
MEMORY_NOTHING_TO_FORGET = {
    "en": "I don't have a stored memory matching that, so there's nothing to forget.",
    "hi": "मेरे पास इससे मेल खाती कोई स्मृति नहीं है, इसलिए भुलाने को कुछ नहीं है।",
    "hinglish": "Mere paas isse match karti koi memory nahi hai, toh bhulane ko kuch nahi hai.",
}
MEMORY_EPHEMERAL = {
    "en": "This is a temporary chat, so I won't store anything — but I've taken it into account for now.",
    "hi": "यह एक अस्थायी चैट है, इसलिए मैं कुछ भी सहेजूँगा नहीं — लेकिन अभी के लिए मैंने इसे ध्यान में रखा है।",
    "hinglish": "Yeh temporary chat hai, isliye main kuch store nahi karunga — par abhi ke liye dhyan mein rakha hai.",
}
MEMORY_BRAIN_DOWN = {
    "en": "My long-term memory is unavailable right now, so I couldn't save that. Please try again later.",
    "hi": "मेरी दीर्घकालिक स्मृति अभी उपलब्ध नहीं है, इसलिए मैं इसे सहेज नहीं सका। कृपया बाद में प्रयास करें।",
    "hinglish": "Meri long-term memory abhi available nahi hai, isliye main ise save nahi kar paya. Baad mein try karein.",
}


def pick(table: dict[str, str], language: str, **kwargs) -> str:
    return table.get(language, table["en"]).format(**kwargs)
