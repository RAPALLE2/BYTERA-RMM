# styles_routes.py (Modul "styles")
from fastapi import APIRouter, Depends

from app.auth import get_current_user
from rmm_mod_styles import registry

router = APIRouter(prefix="/api/styles", tags=["styles"])


@router.get("")
def list_styles(user: dict = Depends(get_current_user)):
    """Alle Styles: der Core-Standard "futuristic" + die Styles dieses Moduls."""
    return {"styles": [{"id": "futuristic", "name": "Futuristisch", "builtin": True,
                        "description": "Standard: Desktop mit Fenstern, Startmenü und Widgets."}]
            + registry.styles()}
