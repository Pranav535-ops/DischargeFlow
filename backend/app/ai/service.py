"""Discharge-summary generation. Phase 3 replaces this with the governed LLM gateway (Bedrock, de-identification)."""
import logging

from fastapi import HTTPException

from app.core.config import settings
from app.patients.models import Patient

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = (
    "You are a medical documentation assistant helping generate professional discharge summaries. "
    "Be thorough, accurate, and use proper medical terminology."
)


def build_summary_prompt(patient: Patient) -> str:
    return f"""Generate a professional hospital discharge summary for the following patient:

Patient Name: {patient.name}
Date of Birth: {patient.date_of_birth}
Admission Date: {patient.admission_date}
Room Number: {patient.room_number}
Primary Diagnosis: {patient.diagnosis}
Attending Physician: {patient.attending_physician}

Please generate a comprehensive discharge summary that includes:
1. Hospital Course Summary
2. Discharge Diagnosis
3. Procedures Performed (if any)
4. Discharge Medications with instructions
5. Activity Restrictions
6. Diet Recommendations
7. Follow-up Instructions
8. Warning Signs to Watch For
9. Emergency Contact Information

Format the summary in a clear, professional medical document style."""


async def generate_summary_text(prompt: str, session_id: str) -> str:
    """Call the configured LLM. Raises 503 when no provider is available instead of failing with a TypeError."""
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except ImportError:
        raise HTTPException(status_code=503, detail="AI provider not installed (emergentintegrations)")
    if not settings.emergent_llm_key:
        raise HTTPException(status_code=503, detail="LLM API key not configured")

    try:
        chat = LlmChat(
            api_key=settings.emergent_llm_key, session_id=session_id, system_message=SYSTEM_MESSAGE
        ).with_model("gemini", "gemini-3-flash-preview")
        return await chat.send_message(UserMessage(text=prompt))
    except Exception as e:
        logger.error("AI generation error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to generate summary")
