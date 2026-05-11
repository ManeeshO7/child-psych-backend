"""
Canonical FHIR system URIs and helper functions for mapping between
GCP Cloud Healthcare FHIR resources.

Keeping these identifiers in one place avoids typos and makes it easier to
evolve the mapping over time.
"""

from __future__ import annotations

from dataclasses import dataclass


TELEPSYCH_BASE_SYSTEM = "https://telepsych.example.com/fhir"


@dataclass(frozen=True, slots=True)
class FhirSystems:
    patient_master: str = f"{TELEPSYCH_BASE_SYSTEM}/Patient"
    allergy: str = f"{TELEPSYCH_BASE_SYSTEM}/AllergyIntolerance"
    condition: str = f"{TELEPSYCH_BASE_SYSTEM}/Condition"
    medication_statement: str = f"{TELEPSYCH_BASE_SYSTEM}/MedicationStatement"
    encounter: str = f"{TELEPSYCH_BASE_SYSTEM}/Encounter"


SYSTEMS = FhirSystems()


def build_identifier(system: str, value: str) -> dict:
    """Small helper to create a FHIR Identifier object."""
    return {"system": system, "value": value}


def add_identifier_if_missing(resource: dict, system: str, value: str) -> None:
    """
    Ensure `resource["identifier"]` contains the given (system, value) pair.
    Mutates the resource in place; safe to call on resources that already have
    identifiers.
    """
    identifiers = list(resource.get("identifier") or [])
    for ident in identifiers:
        if ident.get("system") == system and ident.get("value") == value:
            return
    identifiers.append(build_identifier(system, value))
    resource["identifier"] = identifiers

