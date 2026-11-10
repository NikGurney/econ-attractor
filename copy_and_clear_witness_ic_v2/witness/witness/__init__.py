"""Copy-and-clear witness simulation (standard library only).

The package is split into an institution-agnostic substrate (configuration,
scenario generation, planners, ledger, kernel, validators, comparison
profiles, serialization) and institution plugins under
``witness.institutions``.  Only ``witness.institutions.registry`` and the
application composition root ``witness.app`` import concrete institutions.
"""

__all__ = ["__version__"]

__version__ = "1.1.0"
