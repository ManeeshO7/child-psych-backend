"""
Stripe service: card on file (SetupIntent) and charge (PaymentIntent).
We store only Stripe customer ID on User; card details stay in Stripe.
"""
import os
from typing import Optional

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")


def get_or_create_customer(user_id: str, email: str, existing_stripe_id: Optional[str] = None) -> str:
    """Return Stripe customer ID, creating a customer if needed."""
    if existing_stripe_id:
        return existing_stripe_id
    customer = stripe.Customer.create(email=email, metadata={"user_id": user_id})
    return customer.id


def create_setup_intent(customer_id: str) -> str:
    """Create a SetupIntent for saving a card (no charge). Returns client_secret."""
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=["card"],
        usage="off_session",
    )
    return intent.client_secret


def set_default_payment_method(customer_id: str, payment_method_id: str) -> None:
    """Set the customer's default payment method for future charges."""
    stripe.Customer.modify(customer_id, invoice_settings={"default_payment_method": payment_method_id})


def charge_customer(
    customer_id: str,
    amount_cents: int,
    description: str,
    metadata: Optional[dict] = None,
    price_id: Optional[str] = None,
) -> str:
    """
    Charge the customer's default payment method. Returns payment_intent id.
    Raises stripe.error.CardError etc. on failure.
    
    If price_id is provided, retrieves the Price to get amount; otherwise uses amount_cents.
    """
    # Get the customer to retrieve default payment method
    customer = stripe.Customer.retrieve(customer_id)
    invoice_settings = getattr(customer, "invoice_settings", None)
    payment_method_id = None
    
    if invoice_settings:
        payment_method_id = getattr(invoice_settings, "default_payment_method", None)
    
    if not payment_method_id:
        # Try to get from payment_methods
        payment_methods = stripe.PaymentMethod.list(customer=customer_id, type="card", limit=1)
        if payment_methods.data:
            payment_method_id = payment_methods.data[0].id
        else:
            raise ValueError("Customer has no saved payment method")
    
    # If price_id is provided, retrieve the Price to get the amount
    if price_id:
        price_obj = stripe.Price.retrieve(price_id)
        amount_cents = price_obj.unit_amount or amount_cents
        currency = price_obj.currency or "usd"
    else:
        currency = "usd"
    
    amount_cents = max(50, amount_cents)  # Stripe minimum
    
    intent_params = {
        "amount": amount_cents,
        "currency": currency,
        "customer": customer_id,
        "payment_method": payment_method_id,
        "off_session": True,
        "confirm": True,
        "description": description,
        "metadata": metadata or {},
    }
    
    intent = stripe.PaymentIntent.create(**intent_params)
    return intent.id


def get_visit_price_cents(appointment_type: str) -> int:
    """
    Get visit price in cents for an appointment type.
    Checks env vars: VISIT_PRICE_{TYPE}_CENTS (e.g., VISIT_PRICE_POST_INTAKE_CENTS).
    Falls back to POST_INTAKE_VISIT_AMOUNT_CENTS for post_intake, or 15000 default.
    """
    type_upper = appointment_type.upper().replace("-", "_")
    env_key = f"VISIT_PRICE_{type_upper}_CENTS"
    amount_str = os.environ.get(env_key)
    if amount_str:
        return int(amount_str)
    # Fallback: post_intake uses POST_INTAKE_VISIT_AMOUNT_CENTS
    if appointment_type == "post_intake":
        return int(os.environ.get("POST_INTAKE_VISIT_AMOUNT_CENTS", "15000"))
    # Default for other types
    return int(os.environ.get("VISIT_PRICE_DEFAULT_CENTS", "15000"))


def get_visit_price_id(appointment_type: str) -> Optional[str]:
    """
    Get Stripe Price ID for an appointment type (if using Products/Prices).
    Checks env vars: STRIPE_PRICE_ID_{TYPE} (e.g., STRIPE_PRICE_ID_POST_INTAKE).
    Returns None if not configured (will use amount_cents instead).
    """
    type_upper = appointment_type.upper().replace("-", "_")
    return os.environ.get(f"STRIPE_PRICE_ID_{type_upper}")
