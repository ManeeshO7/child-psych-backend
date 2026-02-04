from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/contact", tags=["contact"])


class ContactBody(BaseModel):
    name: str
    email: str
    phone: str | None = None
    message: str


@router.post("/")
async def contact(body: ContactBody):
    if not body.name.strip() or not body.email.strip() or not body.message.strip():
        raise HTTPException(
            status_code=400,
            detail="Name, email, and message are required",
        )
    # TODO: Send email or store in DB
    return {"success": True}
