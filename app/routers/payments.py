"""
Payments: save card on file (SetupIntent) and confirm. Charge is in appointments router.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_db
from app.dependencies import require_patient
from app.stripe_service import (
    create_setup_intent,
    get_or_create_customer,
    get_visit_price_cents,
    get_visit_price_id,
    set_default_payment_method,
)

router = APIRouter(prefix="/api/payments", tags=["payments"])


def _get_attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


class SetupIntentBody(BaseModel):
    """
    If appointmentId is provided, we validate that appointment belongs to the patient.
    If omitted, we just create a SetupIntent for the logged-in patient (card on file before booking).
    """

    appointmentId: str | None = None


class ConfirmCardBody(BaseModel):
    """
    If appointmentId is provided, we also mark that appointment as card_on_file.
    If omitted, we just mark the patient's Stripe customer as having a default payment method.
    """

    appointmentId: str | None = None
    paymentMethodId: str


def _stripe_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


@router.get("/payment-method")
async def get_payment_method_summary(session: dict = Depends(require_patient)):
    """
    Return a safe summary of the patient's default card on file.
    We never return full card details (only brand/last4/exp).
    """
    if not _stripe_configured():
        raise HTTPException(status_code=503, detail="Payments are not configured (STRIPE_SECRET_KEY)")

    import stripe

    db = await get_db()
    patient = await db.user.find_unique(where={"id": session["id"]})
    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    stripe_id = _get_attr(patient, "stripeCustomerId", "stripe_customer_id")
    if not stripe_id:
        return {"hasCard": False}

    try:
        customer = stripe.Customer.retrieve(stripe_id)
    except Exception:
        # If Stripe customer is missing/invalid, treat as no card on file
        return {"hasCard": False}

    invoice_settings = getattr(customer, "invoice_settings", None)
    pm_id = getattr(invoice_settings, "default_payment_method", None) if invoice_settings else None

    payment_method = None
    if pm_id:
        try:
            payment_method = stripe.PaymentMethod.retrieve(pm_id)
        except Exception:
            payment_method = None

    if not payment_method:
        try:
            pms = stripe.PaymentMethod.list(customer=stripe_id, type="card", limit=1)
            if getattr(pms, "data", None):
                payment_method = pms.data[0]
        except Exception:
            payment_method = None

    if not payment_method:
        return {"hasCard": False}

    card = getattr(payment_method, "card", None)
    if not card:
        return {"hasCard": False}

    return {
        "hasCard": True,
        "brand": getattr(card, "brand", None),
        "last4": getattr(card, "last4", None),
        "expMonth": getattr(card, "exp_month", None),
        "expYear": getattr(card, "exp_year", None),
    }


@router.post("/setup-intent")
async def create_setup_intent_for_appointment(
    body: SetupIntentBody,
    session: dict = Depends(require_patient),
):
    """Create a SetupIntent so the patient can save a card (no charge). Returns clientSecret for Stripe Elements."""
    if not _stripe_configured():
        raise HTTPException(status_code=503, detail="Payments are not configured (STRIPE_SECRET_KEY)")

    db = await get_db()

    # If an appointment is provided, validate ownership and use that patient's record.
    patient = None
    stripe_id = None
    email = ""
    if body.appointmentId:
        appointment = await db.appointment.find_unique(
            where={"id": body.appointmentId},
            include={"patient": True},
        )
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        patient_id = _get_attr(appointment, "patientId", "patient_id")
        if patient_id != session["id"]:
            raise HTTPException(status_code=403, detail="Not your appointment")
        if appointment.status not in ("scheduled", "card_on_file", "pending_confirmation"):
            raise HTTPException(
                status_code=400,
                detail="Appointment is not in a state that allows saving a card",
            )
        patient = appointment.patient
    else:
        # No appointment yet – save card for the logged-in patient so they can book.
        patient = await db.user.find_unique(where={"id": session["id"]})

    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    email = getattr(patient, "email", None) or ""
    stripe_id = _get_attr(patient, "stripeCustomerId", "stripe_customer_id")
    customer_id = get_or_create_customer(
        user_id=session["id"],
        email=email,
        existing_stripe_id=stripe_id,
    )
    if not stripe_id:
        await db.user.update(
            where={"id": session["id"]},
            data={"stripeCustomerId": customer_id},
        )
    client_secret = create_setup_intent(customer_id)
    return {"clientSecret": client_secret}


@router.post("/confirm-card")
async def confirm_card_on_file(
    body: ConfirmCardBody,
    session: dict = Depends(require_patient),
):
    """After Stripe Elements confirms the SetupIntent, set the payment method as default and mark appointment as card on file."""
    if not _stripe_configured():
        raise HTTPException(status_code=503, detail="Payments are not configured (STRIPE_SECRET_KEY)")

    db = await get_db()

    # If an appointment is provided, validate and use its patient.
    patient = None
    if body.appointmentId:
        appointment = await db.appointment.find_unique(
            where={"id": body.appointmentId},
            include={"patient": True},
        )
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")
        patient_id = _get_attr(appointment, "patientId", "patient_id")
        if patient_id != session["id"]:
            raise HTTPException(status_code=403, detail="Not your appointment")
        patient = appointment.patient
    else:
        patient = await db.user.find_unique(where={"id": session["id"]})

    if not patient:
        raise HTTPException(status_code=400, detail="Patient not found")

    stripe_id = _get_attr(patient, "stripeCustomerId", "stripe_customer_id")
    if not stripe_id:
        raise HTTPException(status_code=400, detail="No Stripe customer for this account")
    set_default_payment_method(stripe_id, body.paymentMethodId)
    # If we have an appointment and it's not pending_confirmation, mark as card_on_file.
    # For pending_confirmation, the patient must explicitly confirm via confirm-by-patient.
    if body.appointmentId and appointment and appointment.status != "pending_confirmation":
        await db.appointment.update(
            where={"id": body.appointmentId},
            data={"status": "card_on_file"},
        )
    if body.appointmentId:
        return {"ok": True, "status": "card_on_file"}
    # Card saved for patient, but no specific appointment yet.
    return {"ok": True, "status": "card_on_file"}


@router.get("/pricing")
async def get_visit_pricing():
    """
    Return visit pricing by appointment type (for display to patients).
    No auth required - pricing is public information.
    
    If Stripe Price IDs are configured, fetches amounts from Stripe.
    Otherwise falls back to env-based pricing.
    """
    import stripe

    pricing: dict = {}
    for visit_type in ["intake", "post_intake", "followup"]:
        price_id = get_visit_price_id(visit_type)
        if price_id and os.environ.get("STRIPE_SECRET_KEY"):
            try:
                price_obj = stripe.Price.retrieve(price_id)
                amount_cents = price_obj.unit_amount or 0
            except Exception:
                amount_cents = get_visit_price_cents(visit_type)
        else:
            amount_cents = get_visit_price_cents(visit_type)
        pricing[visit_type] = {
            "amountCents": amount_cents,
            "amountDollars": amount_cents / 100,
            "formatted": f"${amount_cents / 100:.2f}",
        }
    # Duration-based pricing (30/45/60 min) - env VISIT_PRICE_30_CENTS, etc. or STRIPE_PRICE_ID_30
    for duration in [30, 45, 60]:
        key = str(duration)
        price_id = get_visit_price_id(key)
        if price_id and os.environ.get("STRIPE_SECRET_KEY"):
            try:
                price_obj = stripe.Price.retrieve(price_id)
                amount_cents = price_obj.unit_amount or 0
            except Exception:
                amount_cents = get_visit_price_cents(key)
        else:
            amount_cents = get_visit_price_cents(key)
        pricing[key] = {
            "amountCents": amount_cents,
            "amountDollars": amount_cents / 100,
            "formatted": f"${amount_cents / 100:.2f}",
        }
    # Appointment-type pricing (orientation_consult, clinical_intake, followup_med_30, followup_med_therapy_45)
    for apt_type in ["orientation_consult", "clinical_intake", "followup_med_30", "followup_med_therapy_45"]:
        price_id = get_visit_price_id(apt_type)
        if price_id and os.environ.get("STRIPE_SECRET_KEY"):
            try:
                price_obj = stripe.Price.retrieve(price_id)
                amount_cents = price_obj.unit_amount or 0
            except Exception:
                amount_cents = get_visit_price_cents(apt_type)
        else:
            amount_cents = get_visit_price_cents(apt_type)
        pricing[apt_type] = {
            "amountCents": amount_cents,
            "amountDollars": amount_cents / 100,
            "formatted": f"${amount_cents / 100:.2f}",
        }
    return {"pricing": pricing}
