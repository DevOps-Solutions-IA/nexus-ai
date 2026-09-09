"""Messaging persistence (NXS-P09).

Every table is TENANT-OWNED with forced RLS and — where a row references another
messaging or NXS-P06 table — a COMPOSITE TENANT-AWARE foreign key
``(organization_id, <parent_id>) -> (organization_id, id)`` so the database itself
refuses a cross-tenant attachment (ADR-0052). No provider secret is ever stored in a
message or account row; secrets live only in the encrypted vault.
"""
