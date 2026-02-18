#!/usr/bin/env python3
"""
Create Stripe Products and Prices for each appointment type.

Run from backend/ with .env loaded:
  .venv/bin/python scripts/create_stripe_products.py

Options:
  --dry-run       Print what would be created without calling Stripe
  --orientation 7500   Price in cents for orientation (30 min). Default: 7500 ($75)
  --clinical-intake 15000   Price for clinical intake (60 min). Default: 15000 ($150)
  --followup-30 7500   Price for follow-up 30 min. Default: 7500 ($75)
  --followup-45 11250  Price for follow-up 45 min. Default: 11250 ($112.50)

After running, add the printed Price IDs to your backend .env:
  STRIPE_PRICE_ID_ORIENTATION_CONSULT=price_xxx
  STRIPE_PRICE_ID_CLINICAL_INTAKE=price_xxx
  STRIPE_PRICE_ID_FOLLOWUP_MED_30=price_xxx
  STRIPE_PRICE_ID_FOLLOWUP_MED_THERAPY_45=price_xxx
"""
import argparse
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)
os.chdir(backend_dir)

from dotenv import load_dotenv
load_dotenv()

# Appointment type -> (product name, default price cents)
PRODUCTS = {
    "orientation_consult": ("Orientation consultation (30 min)", 7500),
    "clinical_intake": ("Clinical Intake Appointment (75 min)", 15000),
    "followup_med_30": ("Follow-up med management (30 min)", 7500),
    "followup_med_therapy_45": ("Follow-up med + therapy (45 min)", 11250),
}


def main():
    parser = argparse.ArgumentParser(description="Create Stripe products for appointment types")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without creating")
    parser.add_argument("--orientation", type=int, default=7500, help="Orientation price (cents)")
    parser.add_argument("--clinical-intake", type=int, default=15000, dest="clinical_intake", help="Clinical intake price (cents)")
    parser.add_argument("--followup-30", type=int, default=7500, dest="followup_30", help="Follow-up 30 min price (cents)")
    parser.add_argument("--followup-45", type=int, default=11250, dest="followup_45", help="Follow-up 45 min price (cents)")
    args = parser.parse_args()

    prices = {
        "orientation_consult": args.orientation,
        "clinical_intake": args.clinical_intake,
        "followup_med_30": args.followup_30,
        "followup_med_therapy_45": args.followup_45,
    }

    if not os.environ.get("STRIPE_SECRET_KEY"):
        print("Error: STRIPE_SECRET_KEY not set in .env")
        sys.exit(1)

    import stripe
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

    if args.dry_run:
        print("DRY RUN — would create:")
        for apt_type, (name, _) in PRODUCTS.items():
            cents = prices[apt_type]
            print(f"  {apt_type}: {name} — ${cents/100:.2f}")
        return

    results = {}
    for apt_type, (name, _) in PRODUCTS.items():
        amount_cents = prices[apt_type]
        if amount_cents < 50:
            print(f"Skipping {apt_type}: Stripe minimum is 50 cents")
            continue
        try:
            product = stripe.Product.create(
                name=name,
                description=f"TelePsych {name}",
                metadata={"appointment_type": apt_type},
                default_price_data={
                    "currency": "usd",
                    "unit_amount": amount_cents,
                },
            )
            dp = product.default_price
            price_id = dp if isinstance(dp, str) else getattr(dp, "id", str(dp) if dp else None)
            results[apt_type] = price_id
            print(f"Created {apt_type}: {name} — ${amount_cents/100:.2f} -> {price_id}")
        except stripe.error.StripeError as e:
            print(f"Error creating {apt_type}: {e}")
            sys.exit(1)

    print("\nAdd these to your backend .env:")
    print("-" * 50)
    for apt_type, price_id in results.items():
        env_key = f"STRIPE_PRICE_ID_{apt_type.upper()}"
        print(f"{env_key}={price_id}")
    print("-" * 50)


if __name__ == "__main__":
    main()
