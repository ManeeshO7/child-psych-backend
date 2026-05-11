"""
Aggregate import of routers so `main.py` can import a single package.
"""

from . import (  # noqa: F401
    appointments,
    auth,
    availability,
    contact,
    patient_documents,
    patient_forms,
    patient_profile,
    patient_requests,
    payments,
    request_access,
)

