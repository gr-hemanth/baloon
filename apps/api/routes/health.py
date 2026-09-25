from fastapi import APIRouter
from packages.shared.config import settings
from packages.worksheets.answer_engine import get_circuit_breaker

router = APIRouter(tags=["Health"])


@router.get("/health")
def get_health():
    """Health check endpoint."""
    return {"status": "ok"}


@router.get("/status")
@router.get("/api/v1/provider/status")
def get_provider_status():
    """Report AI answer provider status, circuit breaker health, and fallback configuration."""
    order = settings.get_provider_order()
    primary = order[0] if order else "nvidia"
    fallback = order[1] if len(order) > 1 else getattr(settings, "AI_FALLBACK_PROVIDER", "freellm")
    has_key = bool(settings.NVIDIA_API_KEY if primary == "nvidia" else settings.FREELLM_API_KEY)
    cb = get_circuit_breaker(primary)
    return {
        "status": "ok",
        "primary_provider": primary,
        "fallback_provider": fallback,
        "provider_order": order,
        "model": settings.NVIDIA_MODEL if primary == "nvidia" else settings.FREELLM_MODEL,
        "base_url": settings.NVIDIA_BASE_URL if primary == "nvidia" else settings.FREELLM_BASE_URL,
        "configured": has_key,
        "models": settings.get_nvidia_models() if primary == "nvidia" else [settings.FREELLM_MODEL],
        "circuit_breaker": cb.to_dict(),
    }

